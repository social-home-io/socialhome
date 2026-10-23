"""Tests for :mod:`socialhome.services.gfs_publish_retry` — the bounded,
in-memory retry queue for ``POST /gfs/publish``."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from socialhome.services import gfs_publish_retry as mod
from socialhome.services.gfs_publish_retry import (
    GfsPublish,
    GfsPublishRetryQueue,
    PublishOutcome,
    classify_publish_status,
    is_payload_suite_refusal,
    parse_retry_after_s,
)

DELIVERED = PublishOutcome.delivered()
TRANSIENT = PublishOutcome.transient()
PERMANENT = PublishOutcome.permanent()


def _item(n: int = 1, space_id: str = "sp-1") -> GfsPublish:
    return GfsPublish(
        space_id=space_id,
        event_type="space_post_public",
        payload={"space_id": space_id, "n": n},
    )


class _ScriptedSend:
    """``send`` callback answering from a script, recording every call."""

    def __init__(self, script: list[PublishOutcome]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, GfsPublish]] = []

    async def __call__(self, conn_id: str, item: GfsPublish) -> PublishOutcome:
        self.calls.append((conn_id, item))
        return self.script.pop(0) if self.script else DELIVERED


async def _idle(queue: GfsPublishRetryQueue) -> None:
    for _ in range(200):
        if not queue._queues:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("retry queue never drained")


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(mod, "GFS_PUBLISH_RETRY_BACKOFF_S", (0.0, 0.0, 0.0, 0.0))


# ── Status classification ────────────────────────────────────────────────


@pytest.mark.parametrize("status", [200, 201, 204])
def test_2xx_is_delivered(status):
    assert classify_publish_status(status, None).kind == "delivered"


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_408_429_and_5xx_are_transient(status):
    assert classify_publish_status(status, None).kind == "transient"


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 413, 422])
def test_other_statuses_are_permanent(status):
    """A redirect is never followed (``allow_redirects=False``) and a 4xx
    other than 408/429 is the same answer on every attempt."""
    assert classify_publish_status(status, None).kind == "permanent"


def test_429_carries_retry_after():
    outcome = classify_publish_status(429, "60")
    assert outcome.kind == "transient"
    assert outcome.retry_after_s == 60.0


def test_retry_after_parsing():
    assert parse_retry_after_s("12") == 12.0
    assert parse_retry_after_s(" 3 ") == 3.0
    assert parse_retry_after_s(None) is None
    assert parse_retry_after_s("") is None
    assert parse_retry_after_s("-1") is None
    assert parse_retry_after_s("Wed, 21 Oct 2015 07:28:00 GMT") is None
    # A hostile or broken server cannot park a retry for a day.
    assert parse_retry_after_s("86400") == mod.GFS_PUBLISH_MAX_RETRY_AFTER_S


# ── payload_suite refusal ────────────────────────────────────────────────


class _Body:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.asked: int | None = None

    async def read(self, n: int = -1) -> bytes:
        self.asked = n
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


class _Resp:
    def __init__(self, status: int, raw) -> None:
        self.status = status
        self.content = _Body(raw)


@pytest.mark.parametrize(
    ("status", "raw", "expected"),
    [
        (409, b'{"error": "payload_suite_unsupported"}', True),
        (409, b'{"error": "conflict"}', False),
        (409, b"not json", False),
        (409, b'["payload_suite_unsupported"]', False),
        (409, OSError("reset"), False),
        (400, b'{"error": "payload_suite_unsupported"}', False),
        (200, b"{}", False),
    ],
)
async def test_is_payload_suite_refusal(status, raw, expected):
    resp = _Resp(status, raw)
    assert await is_payload_suite_refusal(resp) is expected
    if status == 409:
        # A hostile server can't make us buffer a huge body.
        assert resp.content.asked == mod._REFUSAL_BODY_MAX_BYTES


def test_suite_refused_outcome():
    assert PublishOutcome.suite_refused().kind == "suite_refused"


def test_the_body_picks_the_compressed_copy_only_when_asked_and_present():
    item = GfsPublish(
        space_id="sp", event_type="e", payload={"j": 1}, compressed={"z": 1}
    )
    assert item.body()["payload"] == {"j": 1}
    assert item.body(compressed=True)["payload"] == {"z": 1}
    assert _item().body(compressed=True)["payload"] == _item().payload


async def test_a_queued_item_keeps_a_private_copy_of_both_payloads(fast):
    send = _ScriptedSend([TRANSIENT])
    queue = GfsPublishRetryQueue(send)
    compressed = {"z": [1]}
    queue.enqueue(
        "g1",
        GfsPublish(space_id="sp", event_type="e", payload={}, compressed=compressed),
    )
    compressed["z"].append(2)
    queued = queue._queues["g1"].items[0]
    assert queued.compressed == {"z": [1]}


# ── The queue ────────────────────────────────────────────────────────────


async def test_a_transient_failure_is_retried_until_delivered(fast):
    send = _ScriptedSend([TRANSIENT, DELIVERED])
    queue = GfsPublishRetryQueue(send)
    await queue.start()
    try:
        assert queue.enqueue("g1", _item())
        await _idle(queue)
    finally:
        await queue.stop()
    assert [c[0] for c in send.calls] == ["g1", "g1"]


async def test_a_permanent_failure_drops_only_that_item(fast, caplog):
    send = _ScriptedSend([PERMANENT, DELIVERED])
    queue = GfsPublishRetryQueue(send)
    queue.enqueue("g1", _item(1))
    queue.enqueue("g1", _item(2))
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await queue.start()
        try:
            await _idle(queue)
        finally:
            await queue.stop()
    assert [c[1].payload["n"] for c in send.calls] == [1, 2]
    assert "space_post_public@sp-1" in caplog.text


async def test_items_for_one_gfs_go_out_in_order_behind_a_failing_head(fast):
    send = _ScriptedSend([TRANSIENT, DELIVERED, DELIVERED])
    queue = GfsPublishRetryQueue(send)
    queue.enqueue("g1", _item(1))
    queue.enqueue("g1", _item(2))
    await queue.start()
    try:
        await _idle(queue)
    finally:
        await queue.stop()
    assert [c[1].payload["n"] for c in send.calls] == [1, 1, 2]


async def test_the_queue_gives_up_after_its_budget(fast, caplog):
    budget = len(mod.GFS_PUBLISH_RETRY_BACKOFF_S)
    send = _ScriptedSend([TRANSIENT] * (budget + 5))
    queue = GfsPublishRetryQueue(send)
    queue.enqueue("g1", _item(1))
    queue.enqueue("g1", _item(2, space_id="sp-2"))
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await queue.start()
        try:
            await _idle(queue)
        finally:
            await queue.stop()
    assert len(send.calls) == budget
    assert "giving up on 2 GFS publish(es) to g1" in caplog.text
    assert "space_post_public@sp-1" in caplog.text
    assert "space_post_public@sp-2" in caplog.text


async def test_retry_after_sets_the_next_attempt(fast):
    """A 429's ``Retry-After`` is honoured: the retry is not due before it,
    even when the backoff floor is shorter."""
    queue = GfsPublishRetryQueue(_ScriptedSend([]))
    before = time.monotonic()
    queue.enqueue("g1", _item(), retry_after_s=42.0)
    due = queue._queues["g1"].due_at
    assert 42.0 <= due - before < 43.0


async def test_retry_after_on_a_retry_sets_the_next_attempt(fast):
    send = _ScriptedSend([PublishOutcome.transient(retry_after_s=30.0)])
    queue = GfsPublishRetryQueue(send)
    queue.enqueue("g1", _item())
    await queue.start()
    try:
        for _ in range(100):
            if send.calls:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        due_in = queue._queues["g1"].due_at - time.monotonic()
    finally:
        await queue.stop()
    assert 29.0 < due_in <= 30.0


async def test_a_raising_send_counts_as_transient(fast):
    calls: list[int] = []

    async def _send(conn_id, item):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return DELIVERED

    queue = GfsPublishRetryQueue(_send)
    queue.enqueue("g1", _item())
    await queue.start()
    try:
        await _idle(queue)
    finally:
        await queue.stop()
    assert len(calls) == 2


async def test_the_queued_payload_is_a_private_copy():
    queue = GfsPublishRetryQueue(_ScriptedSend([]))
    payload = {"space_id": "sp-1", "tags": ["a"]}
    queue.enqueue("g1", GfsPublish("sp-1", "space_post_public", payload))
    payload["tags"].append("b")
    assert queue._queues["g1"].items[0].payload == {
        "space_id": "sp-1",
        "tags": ["a"],
    }


async def test_caps_refuse_new_work(monkeypatch, caplog):
    monkeypatch.setattr(mod, "GFS_PUBLISH_RETRY_MAX_PENDING", 2)
    queue = GfsPublishRetryQueue(_ScriptedSend([]))
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        assert queue.enqueue("g1", _item(1))
        assert queue.enqueue("g2", _item(2))
        assert not queue.enqueue("g1", _item(3))
        assert not queue.enqueue("g1", _item(4))
    assert caplog.text.count("retry queue is full") == 1


async def test_pending_reports_a_waiting_gfs():
    queue = GfsPublishRetryQueue(_ScriptedSend([]))
    assert not queue.pending("g1")
    queue.enqueue("g1", _item())
    assert queue.pending("g1")
    assert not queue.pending("g2")


async def test_stop_drops_pending_work_and_refuses_new(caplog):
    queue = GfsPublishRetryQueue(_ScriptedSend([]))
    await queue.start()
    queue.enqueue("g1", _item(), retry_after_s=600.0)
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await queue.stop()
    assert queue._queues == {}
    assert "dropping 1 pending GFS publish" in caplog.text
    assert not queue.enqueue("g1", _item())
    await queue.stop()  # idempotent


async def test_start_is_idempotent_and_restartable(fast):
    send = _ScriptedSend([])
    queue = GfsPublishRetryQueue(send)
    await queue.start()
    first = queue._task
    await queue.start()
    assert queue._task is first
    await queue.stop()
    await queue.start()
    queue.enqueue("g1", _item())
    try:
        await _idle(queue)
    finally:
        await queue.stop()
    assert len(send.calls) == 1


async def test_one_gfs_cannot_crowd_out_the_others(monkeypatch, caplog):
    """A per-connection cap inside the global one: a dead GFS fills its own
    share, and another GFS can still queue."""
    monkeypatch.setattr(mod, "GFS_PUBLISH_RETRY_MAX_PENDING", 10)
    monkeypatch.setattr(mod, "GFS_PUBLISH_RETRY_MAX_PENDING_PER_CONN", 2)
    queue = GfsPublishRetryQueue(_ScriptedSend([]))
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        assert queue.enqueue("dead", _item(1))
        assert queue.enqueue("dead", _item(2))
        assert not queue.enqueue("dead", _item(3))
        assert queue.enqueue("alive", _item(4))
    assert "retry queue for dead is full" in caplog.text
