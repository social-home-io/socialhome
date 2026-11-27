"""SQLite tests for the durable GFS-space item queue (migration 0158)."""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.domain.space_item_outbox import (
    UNPLANNED_GFS,
    UNSENT_QUEUE_FULL,
    UNSENT_REFUSED,
    UNSENT_TOO_LARGE,
    QueuedSpaceItem,
)
from socialhome.repositories.space_item_outbox_repo import SqliteSpaceItemOutboxRepo

pytestmark = pytest.mark.asyncio

T0 = "2026-11-27T10:00:00+00:00"
T1 = "2026-11-27T10:00:05+00:00"
T2 = "2026-11-27T10:01:00+00:00"


def _row(item_id="i1", gfs_id="g1", *, space="sp", due=T0, payload="ct", **kw):
    return QueuedSpaceItem(
        item_id=item_id,
        gfs_id=gfs_id,
        space_id=space,
        epoch=3,
        ring_class=kw.pop("ring_class", "durable"),
        event_type=kw.pop("event_type", "space_chat_message_created"),
        author_user_id=kw.pop("author", "anna"),
        payload=payload,
        next_attempt_at=due,
        created_at=T0,
        **kw,
    )


@pytest.fixture
async def repo(db):
    return SqliteSpaceItemOutboxRepo(db)


async def test_rows_survive_a_new_repo_on_the_same_database(repo, db):
    assert await repo.add([_row(gfs_id="g1"), _row(gfs_id="g2")])
    again = SqliteSpaceItemOutboxRepo(db)
    due = await again.list_due(T0)
    assert [(r.item_id, r.gfs_id, r.payload, r.epoch) for r in due] == [
        ("i1", "g1", "ct", 3),
        ("i1", "g2", "ct", 3),
    ]


async def test_only_due_pending_rows_are_listed_in_due_order(repo):
    await repo.add([_row("late", due=T2), _row("now", due=T0)])
    assert [r.item_id for r in await repo.list_due(T1)] == ["now"]
    assert await repo.next_due_at() == T0
    await repo.delete("now", "g1")
    assert await repo.next_due_at() == T2


async def test_the_space_bound_refuses_and_stores_nothing(repo):
    assert await repo.add([_row("a")], max_items=2, max_bytes=100)
    assert not await repo.add(
        [_row("b", gfs_id="g1"), _row("b", gfs_id="g2")], max_items=2, max_bytes=100
    )
    assert await repo.usage("sp") == (1, 2)
    assert not await repo.add([_row("c", payload="x" * 99)], max_items=9, max_bytes=100)
    # Another space has its own bound.
    assert await repo.add([_row("d", space="other")], max_items=1, max_bytes=100)


async def test_a_newer_latest_only_row_replaces_a_waiting_one(repo):
    await repo.add([_row("pin1", supersede_key="loc:sp:anna", due=T1)])
    await repo.add([_row("pin2", supersede_key="loc:sp:anna", due=T1)])
    await repo.add([_row("bob", supersede_key="loc:sp:bob", due=T1)])
    assert sorted(r.item_id for r in await repo.list_due(T2)) == ["bob", "pin2"]


async def test_backoff_defer_and_expedite(repo):
    await repo.add(
        [
            _row("a", gfs_id="g1"),
            _row("b", gfs_id="g1"),
            _row("c", gfs_id="g2"),
            _row("x", gfs_id="g1", space="other"),
        ]
    )
    await repo.reschedule("a", "g1", attempts=1, next_attempt_at=T2)
    await repo.defer_gfs("g1", "sp", until=T2)
    # Another space on the same GFS is not held behind it.
    assert sorted(r.item_id for r in await repo.list_due(T1)) == ["c", "x"]
    [b] = [r for r in await repo.list_due(T2) if r.item_id == "b"]
    assert b.attempts == 1  # held for its GFS: counts as tried
    await repo.expedite("g1", now=T0)
    assert sorted(r.item_id for r in await repo.list_due(T0)) == ["a", "b", "c", "x"]


async def test_a_reconnect_never_releases_a_budget_slot(repo):
    """An untried row due later waits for its sender-budget slot."""
    await repo.add([_row("slot", gfs_id="g1", due=T2)])
    await repo.expedite("g1", now=T0)
    assert await repo.list_due(T1) == []


async def test_failed_rows_never_fill_the_queue_and_expire(repo):
    await repo.add([_row("a")], max_items=1, max_bytes=100)
    await repo.mark_failed("a", "g1", UNSENT_REFUSED)
    assert await repo.usage("sp") == (0, 0)
    assert await repo.add([_row("b")], max_items=1, max_bytes=100)
    assert await repo.expire_failed("2026-11-27T09:00:00+00:00") == 0
    assert await repo.expire_failed("2026-11-28T00:00:00+00:00") == 1
    assert await repo.list_failed("sp", "anna") == []


async def test_a_stale_item_keeps_no_bytes(repo):
    await repo.add([_row("a")])
    await repo.mark_failed("a", "g1", "seal_failed", drop_payload=True)
    [item] = await repo.list_failed("sp", "anna")
    assert not item.retryable
    assert await repo.retry_failed("sp", "anna", now=T0) == 0


async def test_the_last_row_of_an_item_is_planned_afresh(repo):
    await repo.add([_row("a", gfs_id="g1"), _row("a", gfs_id="g2")])
    assert await repo.count_rows("a") == 2
    await repo.delete("a", "g2")
    await repo.replan("a", "g1", attempts=1, next_attempt_at=T1)
    [row] = await repo.list_due(T1)
    assert (row.gfs_id, row.attempts) == (UNPLANNED_GFS, 1)


async def test_failures_count_on_one_row_per_space_author_and_error(repo):
    for space in ("sp", "sp", "other"):
        await repo.record_failure(
            space_id=space,
            event_type="space_chat_message_created",
            author_user_id="anna",
            error=UNSENT_QUEUE_FULL,
            now=T0,
        )
    [here] = await repo.list_failed("sp", "anna")
    [there] = await repo.list_failed("other", "anna")
    assert (here.count, there.count) == (2, 1)


async def test_an_unplanned_row_expands_into_one_row_per_gfs(repo):
    await repo.add([_row("u", gfs_id=UNPLANNED_GFS)])
    [row] = await repo.list_due(T0)
    await repo.expand(row, ["g1", "g2"], now=T0)
    assert sorted(r.gfs_id for r in await repo.list_due(T0)) == ["g1", "g2"]


async def test_failed_rows_are_the_authors_to_retry_or_discard(repo):
    await repo.add([_row("a"), _row("b", author="ben")])
    await repo.mark_failed("a", "g1", UNSENT_REFUSED)
    await repo.record_failure(
        space_id="sp",
        event_type="space_task_created",
        author_user_id="anna",
        error=UNSENT_TOO_LARGE,
        now=T0,
    )
    for _ in range(3):
        await repo.record_failure(
            space_id="sp",
            event_type="space_chat_message_created",
            author_user_id="anna",
            error=UNSENT_QUEUE_FULL,
            now=T0,
        )
    failed = await repo.list_failed("sp", "anna")
    by_error = {f.error: f for f in failed}
    assert set(by_error) == {UNSENT_REFUSED, UNSENT_TOO_LARGE, UNSENT_QUEUE_FULL}
    assert by_error[UNSENT_REFUSED].retryable
    assert not by_error[UNSENT_TOO_LARGE].retryable
    assert by_error[UNSENT_QUEUE_FULL].count == 3
    assert await repo.list_failed("sp", "ben") == []
    assert [r.item_id for r in await repo.list_due(T0)] == ["b"]

    assert await repo.retry_failed("sp", "anna", now=T0) == 1
    assert sorted(r.item_id for r in await repo.list_due(T0)) == ["a", "b"]
    assert await repo.discard_failed("sp", "anna") == 2
    assert await repo.list_failed("sp", "anna") == []
    assert await repo.count_pending("sp") == 2


async def test_the_schema_refuses_a_pending_row_without_bytes(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.enqueue(
            "INSERT INTO space_item_outbox(item_id, gfs_id, space_id, epoch,"
            " ring_class, event_type, next_attempt_at, created_at)"
            " VALUES('x','g','sp',1,'durable','t',?,?)",
            (T0, T0),
        )
