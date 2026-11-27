"""The durable retry queue of a GFS space's content items (v_97, owner
2026-11-27: retry until accepted, never a legacy fallback)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.gfs_channel import MEMBER_PUBLISH_MAX_PAYLOAD_CHARS
from socialhome.domain.space_item_outbox import (
    UNSENT_QUEUE_FULL,
    UNSENT_REFUSED,
    UNSENT_TOO_LARGE,
)
from socialhome.repositories.space_item_outbox_repo import SqliteSpaceItemOutboxRepo
from socialhome.services import space_item_outbox as mod
from socialhome.services.gfs_publish_retry import PublishOutcome
from socialhome.services.space_item_outbox import SpaceItemOutbox

pytestmark = pytest.mark.asyncio


class _Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class _Gfs:
    """Scripted GFS answers per gfs id; records what it accepted."""

    def __init__(self) -> None:
        self.answers: dict[str, list[PublishOutcome]] = {}
        self.accepted: list[tuple[str, str]] = []
        self.tries: list[str] = []
        self.plan: list[str] | None = ["g1"]

    async def send(self, row) -> PublishOutcome:
        self.tries.append(row.gfs_id)
        script = self.answers.get(row.gfs_id) or []
        outcome = script.pop(0) if script else PublishOutcome.delivered()
        if outcome.kind == "delivered":
            self.accepted.append((row.gfs_id, row.payload))
        return outcome

    async def planner(self, space_id):
        return self.plan


def _outbox(db, clock, gfs, told=None) -> SpaceItemOutbox:
    box = SpaceItemOutbox(SqliteSpaceItemOutboxRepo(db), clock=clock)
    box.attach(sender=gfs.send, planner=gfs.planner)
    if told is not None:

        async def notify(space_id, author):
            told.append((space_id, author))

        box.attach_notify(notify)
    return box


async def _queue(box, payload="ct", gfs_ids=("g1",), **kw):
    return await box.queue(
        space_id=kw.pop("space_id", "sp"),
        epoch=3,
        payload=payload,
        event_type=kw.pop("event_type", "space_chat_message_created"),
        ring_class="durable",
        author_user_id=kw.pop("author", "anna"),
        gfs_ids=list(gfs_ids),
        **kw,
    )


async def test_queued_items_survive_a_restart(db):
    clock, gfs = _Clock(), _Gfs()
    gfs.answers["g1"] = [PublishOutcome.transient()]
    first = _outbox(db, clock, gfs)
    await _queue(first, "m1")
    await first.run_once()
    assert gfs.accepted == []
    # The process restarts: a fresh queue on the same database sends it.
    again = _outbox(db, clock, gfs)
    clock.advance(10)
    await again.run_once()
    assert gfs.accepted == [("g1", "m1")]
    assert await again.waiting("sp") == 0


async def test_a_gfs_that_is_down_waits_then_everything_goes_on_reconnect(db):
    clock, gfs = _Clock(), _Gfs()
    gfs.answers["g1"] = [PublishOutcome.transient()] * 3
    box = _outbox(db, clock, gfs)
    for n in range(3):
        await _queue(box, f"m{n}")
    await box.run_once()
    # The first failure holds the rest of that GFS: one try, not three.
    assert gfs.tries == ["g1"]
    await box.run_once()
    assert gfs.tries == ["g1"]
    clock.advance(6)
    await box.run_once()
    assert gfs.tries == ["g1", "g1"]
    # Back: the reconnect makes them due now; in order.
    gfs.answers["g1"] = []
    await box.expedite("g1")
    await box.run_once()
    assert [p for _g, p in gfs.accepted] == ["m0", "m1", "m2"]


async def test_backoff_never_gives_up(db):
    clock, gfs = _Clock(), _Gfs()
    gfs.answers["g1"] = [PublishOutcome.transient()] * 12
    box = _outbox(db, clock, gfs)
    await _queue(box, "m")
    for _ in range(13):
        await box.run_once()
        clock.advance(601)
    assert gfs.accepted == [("g1", "m")]


async def test_a_429_retry_after_is_honoured(db):
    clock, gfs = _Clock(), _Gfs()
    gfs.answers["g1"] = [PublishOutcome.transient(retry_after_s=120)]
    box = _outbox(db, clock, gfs)
    await _queue(box, "m")
    await box.run_once()
    clock.advance(60)
    await box.run_once()
    assert gfs.accepted == []
    clock.advance(61)
    await box.run_once()
    assert gfs.accepted == [("g1", "m")]


async def test_each_planned_gfs_is_retried_on_its_own(db):
    clock, gfs = _Clock(), _Gfs()
    gfs.answers["g2"] = [PublishOutcome.transient()]
    box = _outbox(db, clock, gfs)
    await _queue(box, "m", gfs_ids=("g1", "g2"))
    await box.run_once()
    assert gfs.accepted == [("g1", "m")]
    clock.advance(6)
    await box.run_once()
    assert gfs.accepted == [("g1", "m"), ("g2", "m")]


async def test_a_permanent_refusal_is_the_authors_to_retry_or_discard(db):
    clock, gfs, told = _Clock(), _Gfs(), []
    gfs.answers["g1"] = [PublishOutcome.permanent()]
    box = _outbox(db, clock, gfs, told)
    await _queue(box, "m")
    await box.run_once()
    assert told == [("sp", "anna")]
    [unsent] = await box.unsent("sp", "anna")
    assert (unsent.error, unsent.retryable) == (UNSENT_REFUSED, True)
    assert await box.waiting("sp") == 0
    # Retry sends it again.
    assert await box.retry("sp", "anna") == 1
    await box.run_once()
    assert gfs.accepted == [("g1", "m")]
    assert await box.unsent("sp", "anna") == []


async def test_discard_forgets_the_failed_items(db):
    clock, gfs, told = _Clock(), _Gfs(), []
    gfs.answers["g1"] = [PublishOutcome.permanent()]
    box = _outbox(db, clock, gfs, told)
    await _queue(box, "m")
    await box.run_once()
    assert await box.discard("sp", "anna") == 1
    assert await box.unsent("sp", "anna") == []


async def test_an_oversize_item_is_refused_at_seal_and_the_author_told(db):
    clock, gfs, told = _Clock(), _Gfs(), []
    box = _outbox(db, clock, gfs, told)
    assert not await _queue(box, "x" * (MEMBER_PUBLISH_MAX_PAYLOAD_CHARS + 1))
    [unsent] = await box.unsent("sp", "anna")
    assert (unsent.error, unsent.retryable) == (UNSENT_TOO_LARGE, False)
    assert told == [("sp", "anna")]
    assert gfs.tries == []


class _SmallRepo(SqliteSpaceItemOutboxRepo):
    __slots__ = ()

    async def add(self, rows, **_kw):
        return await super().add(rows, max_items=2, max_bytes=10_000)


async def test_a_full_queue_refuses_and_the_author_sees_it(db):
    clock, gfs, told = _Clock(), _Gfs(), []
    repo = _SmallRepo(db)
    box = SpaceItemOutbox(repo, clock=clock)
    box.attach(sender=gfs.send, planner=gfs.planner)

    async def notify(space_id, author):
        told.append(author)

    box.attach_notify(notify)
    assert await _queue(box, "a")
    assert await _queue(box, "b")
    assert not await _queue(box, "c")
    assert not await _queue(box, "d")
    full = [u for u in await box.unsent("sp", "anna") if u.error == UNSENT_QUEUE_FULL]
    assert full[0].count == 2 and not full[0].retryable
    assert told == ["anna", "anna"]


async def test_an_unplanned_item_is_planned_when_due(db):
    clock, gfs = _Clock(), _Gfs()
    gfs.plan = []
    box = _outbox(db, clock, gfs)
    await _queue(box, "m", gfs_ids=())
    await box.run_once()
    assert gfs.tries == []
    gfs.plan = ["g1", "g2"]
    clock.advance(6)
    await box.run_once()
    await box.run_once()
    assert sorted(gfs.accepted) == [("g1", "m"), ("g2", "m")]


async def test_a_space_that_left_its_gfs_drops_what_waits(db, caplog):
    clock, gfs = _Clock(), _Gfs()
    gfs.plan = None
    box = _outbox(db, clock, gfs)
    await _queue(box, "m", gfs_ids=())
    await box.run_once()
    assert await box.waiting("sp") == 0
    assert "no longer has a channel" in caplog.text
    # Never silently: its author sees it.
    [unsent] = await box.unsent("sp", "anna")
    assert unsent.error == "refused"


async def test_a_gfs_no_longer_serving_the_space_drops_its_copy_quietly(db):
    """Another GFS still takes the item: this copy just drops."""
    clock, gfs, told = _Clock(), _Gfs(), []
    gfs.answers["g2"] = [PublishOutcome.gone()]
    box = _outbox(db, clock, gfs, told)
    await _queue(box, "m", gfs_ids=("g1", "g2"))
    await box.run_once()
    assert await box.waiting("sp") == 0 and told == []
    assert gfs.accepted == [("g1", "m")]


@pytest.mark.security
async def test_an_items_last_gfs_going_away_is_replanned_then_told(db):
    """Never a change that silently reaches nobody: the last row is planned
    afresh a few times, then its author is told."""
    clock, gfs, told = _Clock(), _Gfs(), []
    gfs.answers["g1"] = [PublishOutcome.gone()] * 10
    box = _outbox(db, clock, gfs, told)
    await _queue(box, "m")
    for _ in range(12):
        await box.run_once()
        clock.advance(601)
    assert told == [("sp", "anna")]
    [unsent] = await box.unsent("sp", "anna")
    assert unsent.retryable
    # A replan found another server meanwhile: it goes there.
    gfs.plan = ["g9"]
    await box.retry("sp", "anna")
    for _ in range(4):
        await box.run_once()
        clock.advance(601)
    assert gfs.accepted == [("g9", "m")]


async def test_an_item_sealed_at_a_lost_epoch_is_discard_only(db):
    clock, gfs, told = _Clock(), _Gfs(), []
    gfs.answers["g1"] = [PublishOutcome.stale()]
    box = _outbox(db, clock, gfs, told)
    await _queue(box, "m")
    await box.run_once()
    [unsent] = await box.unsent("sp", "anna")
    assert (unsent.error, unsent.retryable) == ("seal_failed", False)
    assert told == [("sp", "anna")]


async def test_one_spaces_failing_head_never_holds_another_space_back(db):
    clock, gfs = _Clock(), _Gfs()
    gfs.answers["g1"] = [PublishOutcome.transient()]
    box = _outbox(db, clock, gfs)
    await _queue(box, "a1")
    await _queue(box, "b1", space_id="other")
    await box.run_once()
    assert ("g1", "b1") in gfs.accepted


async def test_a_reconnect_keeps_paced_items_in_their_slots(db):
    clock, gfs = _Clock(), _Gfs()
    box = _outbox(db, clock, gfs)
    await _queue(box, "later", delay_s=30)
    await box.expedite("g1")
    await box.run_once()
    assert gfs.tries == []


async def test_failed_items_nobody_retried_expire_after_a_week(db):
    clock, gfs = _Clock(), _Gfs()
    gfs.answers["g1"] = [PublishOutcome.permanent()]
    box = _outbox(db, clock, gfs)
    await _queue(box, "m")
    await box.run_once()
    assert await box.unsent("sp", "anna")
    clock.advance(8 * 86_400)
    await box.run_once()
    assert await box.unsent("sp", "anna") == []


async def test_a_budget_slot_delays_the_first_try(db):
    clock, gfs = _Clock(), _Gfs()
    box = _outbox(db, clock, gfs)
    await _queue(box, "m", delay_s=5)
    await box.run_once()
    assert gfs.tries == []
    clock.advance(5)
    await box.run_once()
    assert gfs.accepted == [("g1", "m")]


async def test_a_newer_pin_replaces_one_still_waiting(db):
    clock, gfs = _Clock(), _Gfs()
    box = _outbox(db, clock, gfs)
    await _queue(box, "pin1", delay_s=5, supersede_key="loc:sp:anna")
    await _queue(box, "pin2", delay_s=5, supersede_key="loc:sp:anna")
    clock.advance(5)
    await box.run_once()
    assert gfs.accepted == [("g1", "pin2")]


async def test_the_loop_sends_in_the_background_and_stops(db):
    clock, gfs = _Clock(), _Gfs()
    box = _outbox(db, clock, gfs)
    await box.start()
    await _queue(box, "m")
    for _ in range(100):
        if gfs.accepted:
            break
        await asyncio.sleep(0.01)
    await box.stop()
    assert gfs.accepted == [("g1", "m")]


async def test_a_sender_that_raises_backs_off(db, caplog, monkeypatch):
    clock, gfs = _Clock(), _Gfs()

    async def boom(row):
        raise RuntimeError("bug")

    box = SpaceItemOutbox(SqliteSpaceItemOutboxRepo(db), clock=clock)
    box.attach(sender=boom, planner=gfs.planner)
    await _queue(box, "m")
    await box.run_once()
    assert "attempt for space sp failed" in caplog.text
    assert await box.run_once() == 0
    assert mod.BATCH > 0


# ── Review of the 10k sizing (Fable, 2026-11-27) ──────────────────────────


async def test_a_gfs_down_a_while_never_uses_up_the_replans(db):
    """Finding 6a: transient failures, then the GFS leaves the owner's list
    — the item is planned afresh to the replacement, not failed."""
    clock, gfs, told = _Clock(), _Gfs(), []
    gfs.answers["g1"] = [PublishOutcome.transient()] * 3 + [PublishOutcome.gone()]
    box = _outbox(db, clock, gfs, told)
    await _queue(box, "m")
    gfs.plan = ["g9"]
    for _ in range(8):
        await box.run_once()
        clock.advance(601)
    assert told == []
    assert gfs.accepted == [("g9", "m")]


async def test_an_item_that_waited_too_long_is_shown_never_sent(db):
    """Finding 8: receivers would drop it — its author is told instead."""
    clock, gfs, told = _Clock(), _Gfs(), []
    gfs.answers["g1"] = [PublishOutcome.transient()]
    box = _outbox(db, clock, gfs, told)
    await _queue(box, "m")
    await box.run_once()
    clock.advance(13 * 3600)
    await box.run_once()
    assert gfs.accepted == []
    [unsent] = await box.unsent("sp", "anna")
    assert (unsent.error, unsent.retryable) == ("too_old", False)
    assert told == [("sp", "anna")]


async def test_an_item_naming_nobody_is_told_to_the_admins(db, caplog):
    """Finding 9: an item with no named author still surfaces — logged at
    WARNING and handed to the notifier with no author (the app sends it to
    the household's admins)."""

    caplog.set_level(logging.WARNING)
    clock, gfs, told = _Clock(), _Gfs(), []
    gfs.answers["g1"] = [PublishOutcome.permanent()]
    box = _outbox(db, clock, gfs, told)
    await _queue(box, "m", author="")
    await box.run_once()
    assert told == [("sp", "")]
    assert any("names no author" in r.getMessage() for r in caplog.records)
    assert await box.unsent("sp", "ben") == []
    [item] = await box.unsent("sp", "admin", admin=True)
    assert item.error == "refused"
