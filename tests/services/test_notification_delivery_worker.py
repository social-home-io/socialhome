"""Busy households stay fast: :class:`NotificationDeliveryWorker`.

Two halves:

* the worker itself — one job per event, in order, off the publisher's
  path, backpressure instead of drops, the ``asyncio.Event`` lifecycle;
* the fan-out it runs — a space post to 200 members costs a **fixed**
  number of database round-trips (one group lookup, one transaction),
  not several per member, and the publisher pays none of them.

Round-trips are counted on the database itself (every read, queued write
and transaction), so nothing here depends on timing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    NotificationCreated,
    NotificationReadChanged,
    PostCreated,
    SpacePostCreated,
)
from socialhome.domain.mention import Mention, MentionType
from socialhome.domain.notification import NotificationSubject
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.notification_repo import (
    SqliteNotificationRepo,
    new_notification,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_delivery_worker import (
    NotificationDeliveryWorker,
)
from socialhome.services.notification_service import NotificationService
from socialhome.services.push_withdraw_service import PushWithdrawService

# ── The worker ─────────────────────────────────────────────────────────


class _Ev:
    """A stand-in event: the worker only hands it to the handler."""

    def __init__(self, n: int) -> None:
        self.n = n


async def test_before_start_a_job_runs_inline_in_the_publisher():
    seen: list[int] = []

    async def handler(ev):
        seen.append(ev.n)

    worker = NotificationDeliveryWorker()
    await worker.deferred(handler)(_Ev(1))
    assert seen == [1] and not worker.running
    await worker.drain()  # nothing to wait for


async def test_jobs_run_after_the_publisher_returns_in_order():
    seen: list[int] = []

    async def handler(ev):
        seen.append(ev.n)

    worker = NotificationDeliveryWorker()
    await worker.start()
    await worker.start()  # idempotent
    publish = worker.deferred(handler)
    for n in range(5):
        await publish(_Ev(n))
    assert seen == []  # the publisher only queued them
    await worker.drain()
    assert seen == [0, 1, 2, 3, 4]
    await worker.stop()


async def test_a_failing_job_never_stops_the_next(caplog):
    seen: list[int] = []

    async def handler(ev):
        if ev.n == 0:
            raise RuntimeError("boom")
        seen.append(ev.n)

    worker = NotificationDeliveryWorker()
    await worker.start()
    publish = worker.deferred(handler)
    with caplog.at_level(logging.ERROR):
        await publish(_Ev(0))
        await publish(_Ev(1))
        await worker.drain()
    assert seen == [1]
    assert "failed for _Ev" in caplog.text
    await worker.stop()


async def test_a_full_queue_makes_the_publisher_wait_never_drops(caplog):
    gate = asyncio.Event()
    seen: list[int] = []

    async def handler(ev):
        await gate.wait()
        seen.append(ev.n)

    worker = NotificationDeliveryWorker(max_queued=1, full_warning_seconds=0.01)
    await worker.start()
    publish = worker.deferred(handler)
    await publish(_Ev(0))
    await asyncio.sleep(0)  # the worker takes job 0 and blocks on the gate
    await publish(_Ev(1))  # fills the one slot
    assert worker.queued == 1
    with caplog.at_level(logging.WARNING):
        third = asyncio.create_task(publish(_Ev(2)))
        await asyncio.sleep(0.05)
        assert not third.done()  # backpressure: waiting, not dropped
        assert "queue full" in caplog.text
    gate.set()
    await third
    await worker.drain()
    assert seen == [0, 1, 2]
    await worker.stop()


async def test_stop_delivers_what_is_queued_then_runs_inline():
    seen: list[int] = []

    async def handler(ev):
        await asyncio.sleep(0)
        seen.append(ev.n)

    worker = NotificationDeliveryWorker()
    await worker.start()
    publish = worker.deferred(handler)
    for n in range(10):
        await publish(_Ev(n))
    await worker.stop()
    assert seen == list(range(10)) and worker.queued == 0
    await publish(_Ev(10))  # after stop: inline, nothing lost
    assert seen[-1] == 10
    await worker.stop()  # idempotent


async def test_stop_gives_up_on_the_rest_after_the_drain_timeout(caplog):
    async def handler(ev):
        await asyncio.sleep(10)

    worker = NotificationDeliveryWorker(drain_timeout=0.01)
    await worker.start()
    publish = worker.deferred(handler)
    await publish(_Ev(0))
    await publish(_Ev(1))
    await asyncio.sleep(0)
    with caplog.at_level(logging.WARNING):
        await worker.stop()
    assert "undelivered" in caplog.text
    assert not worker.running


async def test_an_event_a_job_publishes_runs_in_that_job_never_deadlocks():
    """A job whose handler publishes another notification event must not
    wait on the queue its own task drains (here: a one-slot queue that is
    already full)."""
    seen: list[str] = []
    worker = NotificationDeliveryWorker(max_queued=1)

    async def inner(ev):
        seen.append("inner")

    async def outer(ev):
        await publish_inner(_Ev(1))
        seen.append("outer")

    publish_inner = worker.deferred(inner)
    await worker.start()
    await worker.deferred(outer)(_Ev(0))
    await worker.deferred(inner)(_Ev(2))  # fills the slot behind outer
    await asyncio.wait_for(worker.drain(), timeout=5)
    assert seen == ["inner", "outer", "inner"]
    await worker.stop()


async def test_the_subscriber_keeps_the_handlers_name():
    async def on_thing(ev):
        return None

    assert (
        NotificationDeliveryWorker()
        .deferred(on_thing)
        .__qualname__.endswith("on_thing")
    )


async def test_a_called_step_runs_after_the_jobs_queued_before_it():
    """``call`` queues a plain step (no event) behind what is queued — a
    step that reads rows earlier jobs write never runs ahead of them."""
    seen: list[str] = []

    async def handler(ev):
        seen.append(f"job{ev.n}")

    async def step():
        seen.append("step")

    worker = NotificationDeliveryWorker()
    await worker.call(step)  # before start: inline
    assert seen == ["step"]
    await worker.start()
    publish = worker.deferred(handler)
    await publish(_Ev(1))
    await worker.call(step)
    await publish(_Ev(2))
    assert seen == ["step"]
    await worker.drain()
    assert seen == ["step", "job1", "step", "job2"]
    await worker.stop()


async def test_a_failing_called_step_is_logged_by_name_and_never_stops_the_next(
    caplog,
):
    seen: list[str] = []

    async def broken_step():
        raise RuntimeError("boom")

    async def step():
        seen.append("step")

    worker = NotificationDeliveryWorker()
    await worker.start()
    with caplog.at_level(logging.ERROR):
        await worker.call(broken_step)
        await worker.call(step)
        await worker.drain()
    assert seen == ["step"]
    assert "broken_step failed for" in caplog.text
    await worker.stop()


async def test_a_step_waiting_on_a_full_queue_is_named_in_the_warning(caplog):
    gate = asyncio.Event()

    async def hold():
        await gate.wait()

    async def waiting_step():
        return None

    worker = NotificationDeliveryWorker(max_queued=1, full_warning_seconds=0.01)
    await worker.start()
    await worker.call(hold)  # in hand
    await asyncio.sleep(0)
    await worker.call(hold)  # fills the one slot
    with caplog.at_level(logging.WARNING):
        late = asyncio.create_task(worker.call(waiting_step))
        while "waits for a free slot" not in caplog.text:
            await asyncio.sleep(0.01)
    assert "waiting_step waits for a free slot" in caplog.text
    gate.set()
    await late
    await worker.drain()
    await worker.stop()


# ── The fan-out it runs: round-trips per job ───────────────────────────


class _CountingDatabase(AsyncDatabase):
    """Counts round-trips: every read, queued write and transaction."""

    round_trips = 0

    async def _read(self, fn):
        type(self).round_trips += 1
        return await super()._read(fn)

    async def enqueue(self, sql, params=()):
        type(self).round_trips += 1
        return await super().enqueue(sql, params)

    async def enqueue_rowcount(self, sql, params=()):
        type(self).round_trips += 1
        return await super().enqueue_rowcount(sql, params)

    async def transact(self, fn):
        type(self).round_trips += 1
        return await super().transact(fn)


SP = "sp-busy"
AUTHOR = "u-author"


async def _household(tmp_dir, members: int, *, worker: bool):
    class Db(_CountingDatabase):
        round_trips = 0

    db = Db(tmp_dir / f"busy-{members}-{worker}.db", batch_timeout_ms=1)
    await db.startup()
    bus = EventBus()
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0e" * 32))
    users = SqliteUserRepo(db)
    notifs = SqliteNotificationRepo(db)
    await spaces.save(
        Space(
            id=SP,
            name="Busy",
            owner_instance_id="iid",
            owner_username="author",
            identity_public_key="00" * 32,
            config_sequence=1,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,  # D5: "Everything" — a row each
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    ids = [AUTHOR] + [f"u-{i:03d}" for i in range(members)]
    for uid in ids:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (uid, uid, uid.title()),
        )
        await spaces.save_member(
            SpaceMember(
                space_id=SP,
                user_id=uid,
                role=SpaceRole.MEMBER.value,
                joined_at="2026-01-01T00:00:00+00:00",
            )
        )
    svc = NotificationService(notifs, users, spaces, bus)
    w = NotificationDeliveryWorker() if worker else None
    if w is not None:
        svc.attach_delivery_worker(w)
        await w.start()
    svc.wire()
    created: list[NotificationCreated] = []

    async def _nc(ev):
        created.append(ev)

    bus.subscribe(NotificationCreated, _nc)
    return db, bus, svc, notifs, w, created, ids[1:]


def _post(pid: str, content: str = "hello") -> SpacePostCreated:
    return SpacePostCreated(
        post=Post(
            id=pid,
            author=AUTHOR,
            type=PostType.TEXT,
            content=content,
            created_at=datetime.now(UTC),
        ),
        space_id=SP,
    )


async def _cost(db, coro) -> int:
    before = type(db).round_trips
    await coro
    return type(db).round_trips - before


@pytest.mark.parametrize("members", [20, 200])
async def test_the_publisher_returns_before_any_per_recipient_work(tmp_dir, members):
    db, bus, _svc, notifs, worker, created, ids = await _household(
        tmp_dir, members, worker=True
    )
    try:
        assert await _cost(db, bus.publish(_post("p1"))) == 0
        assert created == [] and worker.queued == 1  # one job, not one each
        await worker.drain()
        assert len(created) == members
        for uid in ids[:3]:
            assert len(await notifs.list(uid)) == 1
    finally:
        await worker.stop()
        await db.shutdown()


async def test_a_fan_out_costs_the_same_round_trips_for_20_or_200(tmp_dir):
    """Before: each member cost a level read, two user reads, a group
    lookup and an insert + prune — 1 000+ round-trips for 200 members.
    Now one job costs a fixed handful, whatever the size of the space."""
    costs: dict[int, tuple[int, int, int]] = {}
    for members in (20, 200):
        db, bus, _svc, _n, worker, created, _ids = await _household(
            tmp_dir, members, worker=True
        )
        try:
            await bus.publish(_post("p1"))
            first = await _cost(db, worker.drain())  # new rows
            await bus.publish(_post("p2"))
            again = await _cost(db, worker.drain())  # the same rows, regrouped
            # An @here: a push-level row about the post, for everyone.
            await bus.publish(
                SpacePostCreated(
                    post=_post("p3").post,
                    space_id=SP,
                    mentions=tuple(
                        Mention(type=MentionType.USER, raw=u, user_id=u) for u in _ids
                    ),
                )
            )
            mention = await _cost(db, worker.drain())
            costs[members] = (first, again, mention)
            assert len(created) == 3 * members
        finally:
            await worker.stop()
            await db.shutdown()
    assert costs[20] == costs[200], costs
    assert max(costs[200]) <= 12, costs


async def test_one_save_at_a_time_grows_with_the_space(tmp_dir):
    """The same handler without a delivery job (called directly, as a test
    or a route may) still writes one row at a time — the cost the worker
    takes away."""
    db, _bus, svc, _n, _w, created, _ids = await _household(tmp_dir, 50, worker=False)
    try:
        cost = await _cost(db, svc.on_space_post_created(_post("p1")))
        assert len(created) == 50 and cost >= 4 * 50
    finally:
        await db.shutdown()


async def test_a_household_post_fan_out_is_one_job_too(tmp_dir):
    db, bus, _svc, notifs, worker, created, ids = await _household(
        tmp_dir, 200, worker=True
    )
    try:
        post = _post("h1").post
        assert await _cost(db, bus.publish(PostCreated(post=post))) == 0
        cost = await _cost(db, worker.drain())
        assert len(created) == 200 and cost <= 8
        assert [n.type for n in await notifs.list(ids[0])] == ["post_created"]
    finally:
        await worker.stop()
        await db.shutdown()


async def test_a_reopened_fan_out_counts_unread_in_one_read(tmp_dir):
    db, bus, _svc, notifs, worker, _created, ids = await _household(
        tmp_dir, 200, worker=True
    )
    changed: list[NotificationReadChanged] = []

    async def _rc(ev):
        changed.append(ev)

    bus.subscribe(NotificationReadChanged, _rc)
    try:
        await bus.publish(_post("p1"))
        await worker.drain()
        for uid in ids:
            await notifs.mark_all_read(uid)
        await bus.publish(_post("p2"))
        cost = await _cost(db, worker.drain())
        assert len(changed) == 200 and {e.unread_count for e in changed} == {1}
        assert cost <= 12
    finally:
        await worker.stop()
        await db.shutdown()


# ── One job = one batch: the same answers as one save at a time ─────────


class _Push:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def push_to_user(self, user_id, payload) -> None:
        self.sent.append((user_id, payload.tag))


def _note(user_id: str, key: str = "post:p1", ntype: str = "space_mention"):
    return (
        new_notification(user_id=user_id, type=ntype, title=ntype, link_url="/x"),
        NotificationSubject(key),
    )


async def test_two_events_for_one_group_in_one_job_make_one_row(tmp_dir):
    """The second joins the row the first one made — bumped and silent
    (D7, D8), as when each was saved on its own."""
    db, _bus, svc, notifs, _w, created, ids = await _household(tmp_dir, 2, worker=False)
    push = _Push()
    svc.attach_push_service(push)

    async def handler(_ev):
        for _ in range(2):
            note, about = _note(ids[0])
            await svc._save_notif(note, about=about, actor_user_id=AUTHOR)
            await svc._send_push(
                await svc._local_user(ids[1]),
                ntype="dm_message",
                title="t",
                link_url="/dms/c",
                tag="dm",
            )

    try:
        await svc.run_batched(handler, _Ev(0))
        rows = await notifs.list(ids[0])
        assert len(rows) == 1 and rows[0].params.total == 2
        assert [(e.bumped, e.silent) for e in created] == [
            (False, False),
            (True, True),
        ]
        # Pushes leave in the order the handler made them; the repeat is
        # silent.
        assert push.sent == [
            (ids[0], "space_mention:/x"),
            (ids[1], "dm"),
            (ids[1], "dm"),
        ]
    finally:
        await db.shutdown()


async def test_a_handler_that_fails_still_writes_what_it_gathered(tmp_dir):
    db, _bus, svc, notifs, _w, created, ids = await _household(tmp_dir, 1, worker=False)

    async def handler(_ev):
        note, about = _note(ids[0])
        await svc._save_notif(note, about=about, actor_user_id=AUTHOR)
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError):
            await svc.run_batched(handler, _Ev(0))
        assert len(await notifs.list(ids[0])) == 1 and len(created) == 1
    finally:
        await db.shutdown()


async def test_a_nested_job_joins_the_outer_batch(tmp_dir):
    db, _bus, svc, notifs, _w, _created, ids = await _household(
        tmp_dir, 1, worker=False
    )

    async def inner(_ev):
        note, about = _note(ids[0])
        await svc._save_notif(note, about=about, actor_user_id=AUTHOR)
        assert await notifs.list(ids[0]) == []  # the outer job writes it

    async def outer(ev):
        await svc.run_batched(inner, ev)

    try:
        await svc.run_batched(outer, _Ev(0))
        assert len(await notifs.list(ids[0])) == 1
    finally:
        await db.shutdown()


async def test_a_row_that_fails_to_land_is_never_announced(tmp_dir):
    """Its user went away between the gate and the write: the rest of the
    fan-out lands, and only the rows that landed are announced."""
    db, _bus, svc, notifs, _w, created, ids = await _household(tmp_dir, 2, worker=False)

    async def handler(_ev):
        for uid in ids:
            note, about = _note(uid)
            await svc._save_notif(note, about=about, actor_user_id=AUTHOR)
        await db.enqueue("DELETE FROM space_members WHERE user_id=?", (ids[1],))
        await db.enqueue("DELETE FROM users WHERE user_id=?", (ids[1],))

    try:
        await svc.run_batched(handler, _Ev(0))
        assert [e.user_id for e in created] == [ids[0]]
        assert len(await notifs.list(ids[0])) == 1
    finally:
        await db.shutdown()


async def test_a_batched_fan_out_push_is_recorded_for_withdrawal(tmp_dir):
    """Every push the worker's batch sends is noted with the tag the row
    carries, so reading the row later takes it off the phone (#1223)."""
    db, bus, svc, notifs, worker, _created, ids = await _household(
        tmp_dir, 20, worker=True
    )
    withdrawals = PushWithdrawService(SqliteUserRepo(db))
    svc.attach_push_withdraw(withdrawals)
    push = _Push()
    svc.attach_push_service(push)
    try:
        # @-mentioning everyone: a push-level row for each member.
        await bus.publish(
            SpacePostCreated(
                post=_post("p-m").post,
                space_id=SP,
                mentions=tuple(
                    Mention(type=MentionType.USER, raw=u, user_id=u) for u in ids
                ),
            )
        )
        await worker.drain()
        assert len(push.sent) == len(ids)
        for uid in ids:
            [row] = await notifs.list(uid)
            tag = f"{row.type}:{row.link_url or row.id}"
            assert (uid, tag) in push.sent
            # Recorded: a withdraw of that tag goes out (only pushed tags do).
            assert withdrawals.withdraw(uid, [tag]) == [tag]
            assert withdrawals.withdraw(uid, ["never:pushed"]) == []
    finally:
        await worker.stop()
        await db.shutdown()
