"""Direct SQLite tests for ``SqliteDmMediaOutboxRepo``.

Most of the repo is already exercised through
``test_dm_media_sync_service`` via an in-memory fake. This file
hits the SQLite paths directly so the migration + the literal SQL
get coverage too: the scheduler runs against this implementation in
production.
"""

from __future__ import annotations

import pytest

from socialhome.repositories.dm_media_outbox_repo import (
    SqliteDmMediaOutboxRepo,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def media_outbox(db):
    """Seed a conversation + a message row (so the FK on
    ``dm_media_outbox.message_id`` is satisfied), return the repo
    handle bound to the live DB."""
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    await db.enqueue(
        """
        INSERT INTO conversation_messages(
            id, conversation_id, sender_user_id, content, type,
            created_at
        ) VALUES(?,?,?,?,?, datetime('now'))
        """,
        ("m-1", "conv-1", "u-alice", "", "image"),
    )
    return SqliteDmMediaOutboxRepo(db), db


async def test_enqueue_and_list_due(media_outbox):
    """A freshly enqueued row is immediately due (default ts in past)."""
    repo, _db = media_outbox
    await repo.enqueue(
        blob_id="m-1",
        message_id="m-1",
        target_instance_id="inst-bob",
        bytes_path="/tmp/foo.bin",
    )
    due = await repo.list_due()
    assert len(due) == 1
    assert due[0].blob_id == "m-1"
    assert due[0].target_instance_id == "inst-bob"
    assert due[0].status == "pending"


async def test_enqueue_idempotent(media_outbox):
    """Re-enqueueing the same (blob, target) pair is a no-op."""
    repo, _db = media_outbox
    await repo.enqueue(
        blob_id="m-1",
        message_id="m-1",
        target_instance_id="inst-bob",
        bytes_path="/tmp/v1.bin",
    )
    await repo.enqueue(
        blob_id="m-1",
        message_id="m-1",
        target_instance_id="inst-bob",
        bytes_path="/tmp/v2.bin",
    )
    due = await repo.list_due()
    assert len(due) == 1
    # The first insert's bytes_path stays (ON CONFLICT DO NOTHING).
    assert due[0].bytes_path == "/tmp/v1.bin"


async def test_mark_in_flight_hides_from_due(media_outbox):
    """``in_flight`` rows are excluded from ``list_due``."""
    repo, _db = media_outbox
    await repo.enqueue(
        blob_id="m-1",
        message_id="m-1",
        target_instance_id="inst-bob",
        bytes_path="/tmp/foo.bin",
    )
    await repo.mark_in_flight(blob_id="m-1", target_instance_id="inst-bob")
    assert await repo.list_due() == []


async def test_reclaim_in_flight_flips_back_to_pending(media_outbox):
    """The startup reaper flips stuck in_flight rows back."""
    repo, _db = media_outbox
    await repo.enqueue(
        blob_id="m-1",
        message_id="m-1",
        target_instance_id="inst-bob",
        bytes_path="/tmp/foo.bin",
    )
    await repo.mark_in_flight(blob_id="m-1", target_instance_id="inst-bob")
    stuck = await repo.reclaim_in_flight()
    assert stuck == 1
    # Row is pending again but pushed out 10 s — not immediately due.
    immediate = await repo.list_due()
    assert immediate == []
    # ``list_for_message`` ignores the time gate.
    rows = await repo.list_for_message("m-1")
    assert len(rows) == 1
    assert rows[0].status == "pending"


async def test_reclaim_in_flight_returns_zero_when_clean(media_outbox):
    """No stuck rows → returns 0 without touching the table."""
    repo, _db = media_outbox
    assert await repo.reclaim_in_flight() == 0


async def test_reschedule_pushes_next_attempt(media_outbox):
    """Reschedule bumps attempts + next_attempt_at + records the error."""
    repo, _db = media_outbox
    await repo.enqueue(
        blob_id="m-1",
        message_id="m-1",
        target_instance_id="inst-bob",
        bytes_path="/tmp/foo.bin",
    )
    await repo.reschedule(
        blob_id="m-1",
        target_instance_id="inst-bob",
        attempts=3,
        next_attempt_at="2099-01-01 00:00:00",  # far future
        last_error="boom",
    )
    immediate = await repo.list_due()
    assert immediate == []  # not yet due
    rows = await repo.list_for_message("m-1")
    assert rows[0].attempts == 3
    assert rows[0].status == "pending"
    assert rows[0].last_error == "boom"


async def test_mark_failed_and_list_for_message(media_outbox):
    """Failed terminal state surfaces in ``list_for_message``."""
    repo, _db = media_outbox
    await repo.enqueue(
        blob_id="m-1",
        message_id="m-1",
        target_instance_id="inst-bob",
        bytes_path="/tmp/foo.bin",
    )
    await repo.mark_failed(
        blob_id="m-1",
        target_instance_id="inst-bob",
        last_error="exhausted",
    )
    rows = await repo.list_for_message("m-1")
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].last_error == "exhausted"
    # Failed rows aren't due-now either.
    assert await repo.list_due() == []


async def test_delete_removes_row(media_outbox):
    """Successful dispatch deletes the row."""
    repo, _db = media_outbox
    await repo.enqueue(
        blob_id="m-1",
        message_id="m-1",
        target_instance_id="inst-bob",
        bytes_path="/tmp/foo.bin",
    )
    await repo.delete(blob_id="m-1", target_instance_id="inst-bob")
    assert await repo.list_due() == []
    assert await repo.list_for_message("m-1") == []


async def test_delete_for_instance_drops_every_row_for_that_household(media_outbox):
    """An unpair tombstone gets nothing but our UNPAIR: every media row
    addressed to it goes, whatever its status; other households keep
    theirs."""
    repo, _db = media_outbox
    for target in ("inst-gone", "inst-kept"):
        await repo.enqueue(
            blob_id="m-1",
            message_id="m-1",
            target_instance_id=target,
            bytes_path="/tmp/foo.bin",
        )
    await repo.mark_failed(
        blob_id="m-1", target_instance_id="inst-gone", last_error="x"
    )

    await repo.delete_for_instance("inst-gone")

    rows = await repo.list_for_message("m-1")
    assert [r.target_instance_id for r in rows] == ["inst-kept"]
