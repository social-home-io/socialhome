"""The GFS relay's binary media frames (v_87): deliver-or-queue, separate
budgets, never at the control envelopes' expense, and cross-node drain over
the shared queue."""

from __future__ import annotations

import asyncio
import time

import pytest

from socialhome.domain.relay_media_frame import build_relay_media_body
from socialhome.global_server import envelope_relay as envelope_relay_mod
from socialhome.global_server.config import EnvelopeMediaConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.envelope_relay import (
    ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    GfsEnvelopeRelay,
    SlidingByteBudget,
)
from socialhome.global_server.repositories import (
    SqliteGfsEnvelopeQueueRepo,
    SqliteGfsFederationRepo,
)

RECIPIENT = "recipient2home2222222222222222aa"
SEALED = {"kem_suite": "x25519", "eph_pk": "ZXBo", "ciphertext": "bm9uY2U:Y3Q"}
KIB = 1024


def _media(n: int = 100 * KIB, fill: bytes = b"\x07") -> bytes:
    return build_relay_media_body(RECIPIENT, fill * n)


class _Registry:
    """``GfsWebSocketRegistry`` stand-in with the v_87 media surface."""

    def __init__(self, *, online=(), media=()) -> None:
        self.online = set(online)
        self.media = set(media)
        self.sent: list[tuple[str, dict]] = []
        self.sent_media: list[tuple[str, bytes]] = []

    async def send(self, instance_id, payload) -> bool:
        if instance_id not in self.online:
            return False
        self.sent.append((instance_id, payload))
        return True

    async def send_media(self, instance_id, body) -> bool:
        if instance_id not in self.online or instance_id not in self.media:
            return False
        self.sent_media.append((instance_id, body))
        return True

    def takes_media(self, instance_id) -> bool:
        return instance_id in self.online and instance_id in self.media

    def is_connected(self, instance_id) -> bool:
        return instance_id in self.online


class _Cluster:
    def __init__(self) -> None:
        self.hinted: list[str] = []

    def hint_drain(self, instance_id: str) -> None:
        self.hinted.append(instance_id)


def _relay(gfs_db, registry, **media) -> GfsEnvelopeRelay:
    return GfsEnvelopeRelay(
        fed_repo=SqliteGfsFederationRepo(gfs_db),
        queue_repo=SqliteGfsEnvelopeQueueRepo(gfs_db),
        ws_registry=registry,
        media=EnvelopeMediaConfig(**media),
    )


@pytest.fixture
async def seeded(gfs_db):
    await SqliteGfsFederationRepo(gfs_db).upsert_instance(
        ClientInstance(
            instance_id=RECIPIENT,
            display_name="R",
            public_key="aa" * 32,
            status="active",
        )
    )
    return gfs_db


async def test_live_socket_with_the_label_gets_the_body_unchanged(seeded):
    reg = _Registry(online={RECIPIENT}, media={RECIPIENT})
    relay = _relay(seeded, reg)
    body = _media()
    await relay.accept_media(RECIPIENT, body)
    assert reg.sent_media == [(RECIPIENT, body)]
    assert reg.sent == []


async def test_socket_without_the_label_keeps_it_queued(seeded):
    reg = _Registry(online={RECIPIENT})
    relay = _relay(seeded, reg)
    await relay.accept_media(RECIPIENT, _media())
    assert reg.sent_media == []
    queue = SqliteGfsEnvelopeQueueRepo(seeded)
    assert await queue.media_bytes_for(RECIPIENT, now=0) > 0
    # Draining to a socket that never listed the label sends nothing.
    await relay.drain(RECIPIENT)
    await relay.settle_media()
    assert reg.sent_media == []
    # Once the household reconnects with it, the frame goes out.
    reg.media.add(RECIPIENT)
    await relay.drain(RECIPIENT)
    await relay.settle_media()
    assert len(reg.sent_media) == 1
    assert await queue.media_bytes_for(RECIPIENT, now=0) == 0


async def test_unknown_recipient_stores_nothing(seeded):
    reg = _Registry()
    relay = _relay(seeded, reg)
    await relay.accept_media("stranger2home2222222222222222abc", _media())
    queue = SqliteGfsEnvelopeQueueRepo(seeded)
    assert await queue.media_bytes_for("stranger2home2222222222222222abc", now=0) == 0


async def test_offline_media_drains_after_the_envelopes_in_order(seeded):
    reg = _Registry()
    relay = _relay(seeded, reg)
    first, second = _media(fill=b"\x01"), _media(fill=b"\x02")
    await relay.accept_media(RECIPIENT, first)
    await relay.accept(RECIPIENT, SEALED)
    await relay.accept_media(RECIPIENT, second)
    reg.online.add(RECIPIENT)
    reg.media.add(RECIPIENT)
    assert await relay.drain(RECIPIENT) == 1  # control first
    await relay.settle_media()
    assert [p["type"] for _i, p in reg.sent] == ["envelope"]
    assert [b for _i, b in reg.sent_media] == [first, second]


@pytest.mark.security
async def test_media_never_evicts_or_crowds_out_control_envelopes(seeded):
    """The media queue is its own table with its own caps: a recipient whose
    media budget is full still gets every control envelope queued, and a
    media flood never deletes an envelope."""
    reg = _Registry()
    relay = _relay(seeded, reg, media_queue_bytes_per_recipient=600 * KIB)
    queue = SqliteGfsEnvelopeQueueRepo(seeded)
    await relay.accept(RECIPIENT, SEALED)
    for _ in range(10):  # 10 × ~100 KiB against a 600 KiB media cap
        await relay.accept_media(RECIPIENT, _media())
    assert await queue.media_bytes_for(RECIPIENT, now=0) <= 600 * KIB
    for _ in range(5):
        await relay.accept(RECIPIENT, SEALED)
    assert await queue.count_for(RECIPIENT) == 6
    rows = await queue.list_for(RECIPIENT, now=0)
    assert [r.sealed for r in rows] == [SEALED] * 6


async def test_server_wide_media_cap_tail_drops(seeded):
    reg = _Registry()
    relay = _relay(
        seeded,
        reg,
        media_queue_max_total_bytes=528 * KIB,
        media_queue_bytes_per_recipient=16 * 1024 * KIB,
    )
    queue = SqliteGfsEnvelopeQueueRepo(seeded)
    for _ in range(8):
        await relay.accept_media(RECIPIENT, _media())
    held = await queue.media_bytes_for(RECIPIENT, now=0)
    assert 0 < held <= 528 * KIB


async def test_an_offline_media_enqueue_hints_the_cluster(seeded):
    reg = _Registry()
    relay = _relay(seeded, reg)
    cluster = _Cluster()
    relay.attach_cluster(cluster)
    await relay.accept_media(RECIPIENT, _media())
    assert cluster.hinted == [RECIPIENT]


async def test_cross_node_media_arrives_through_the_shared_queue(seeded):
    """Posted on node A, the recipient's socket on node B: A queues it in
    the shared database and hints; B's drain pushes the same bytes."""
    reg_a, reg_b = _Registry(), _Registry(online={RECIPIENT}, media={RECIPIENT})
    node_a, node_b = _relay(seeded, reg_a), _relay(seeded, reg_b)
    cluster = _Cluster()
    node_a.attach_cluster(cluster)
    body = _media(300 * KIB)
    await node_a.accept_media(RECIPIENT, body)
    assert cluster.hinted == [RECIPIENT]
    await node_b.drain(RECIPIENT)
    await node_b.settle_media()
    assert reg_b.sent_media == [(RECIPIENT, body)]
    await node_b.drain(RECIPIENT)
    await node_b.settle_media()
    assert reg_b.sent_media == [(RECIPIENT, body)]  # never twice


async def test_submit_media_runs_in_the_background_and_bounds_bytes(seeded):
    reg = _Registry(online={RECIPIENT}, media={RECIPIENT})
    relay = _relay(seeded, reg, media_max_inflight_bytes=528 * KIB)
    big = _media(400 * KIB)
    relay.submit_media(RECIPIENT, big)
    assert relay.media_inflight_bytes == len(big)
    relay.submit_media(RECIPIENT, big)  # past the in-flight byte bound: dropped
    await relay.close()
    assert reg.sent_media == [(RECIPIENT, big)]
    assert relay.media_inflight_bytes == 0
    # The envelope slots were never touched.
    assert relay.in_flight == 0


async def test_media_budget_allows_then_refuses(seeded):
    relay = _relay(seeded, _Registry(), media_ip_bytes_per_minute=1024 * KIB)
    assert relay.media_budget_allows("1.2.3.4", 600 * KIB)
    assert not relay.media_budget_allows("1.2.3.4", 600 * KIB)
    assert relay.media_budget_allows("5.6.7.8", 600 * KIB)


def test_byte_budget_window_slides_and_keys_are_bounded():
    budget = SlidingByteBudget(100, window_s=60.0, max_keys=2)
    assert budget.allow("a", 80, now=0.0)
    assert not budget.allow("a", 30, now=10.0)
    assert budget.allow("a", 30, now=61.0)
    budget.allow("b", 1, now=62.0)
    budget.allow("c", 1, now=63.0)
    assert len(budget) == 2


def test_media_queue_default_is_far_below_the_envelope_byte_cap():
    assert EnvelopeMediaConfig().media_queue_bytes_per_recipient < (
        ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT
    )


async def test_media_queue_expires_and_is_pruned(seeded):
    queue = SqliteGfsEnvelopeQueueRepo(seeded)
    assert await queue.enqueue_media(
        RECIPIENT,
        b"x" * 10,
        created_at=100,
        expires_at=200,
        max_bytes_per_recipient=1000,
        max_total_bytes=1000,
    )
    assert await queue.next_media_for(RECIPIENT, now=150) is not None
    assert await queue.next_media_for(RECIPIENT, now=250) is None
    assert await queue.prune_expired_media(250) == 1
    assert await queue.media_bytes_for(RECIPIENT, now=0) == 0


# ── Control first: the media drain never holds up envelopes ──────────────


class _SlowMediaRegistry(_Registry):
    def __init__(self, delay: float, **kw) -> None:
        super().__init__(**kw)
        self.delay = delay

    async def send_media(self, instance_id, body) -> bool:
        await asyncio.sleep(self.delay)
        return await super().send_media(instance_id, body)


async def test_control_drains_promptly_while_media_is_queued(seeded):
    reg = _SlowMediaRegistry(1.0)
    relay = _relay(seeded, reg)
    for _ in range(3):
        await relay.accept_media(RECIPIENT, _media())
    await relay.accept(RECIPIENT, SEALED)
    reg.online.add(RECIPIENT)
    reg.media.add(RECIPIENT)
    started = time.monotonic()
    assert await relay.drain(RECIPIENT) == 1  # the envelope, at once
    assert time.monotonic() - started < 0.5
    assert [p["type"] for _i, p in reg.sent] == ["envelope"]
    assert reg.sent_media == []  # media follows in the background
    await relay.settle_media()
    assert len(reg.sent_media) == 3


async def test_one_media_drain_per_recipient_at_a_time(seeded):
    reg = _SlowMediaRegistry(0.05, online={RECIPIENT}, media={RECIPIENT})
    relay = _relay(seeded, reg)
    reg.online.discard(RECIPIENT)
    bodies = [_media(fill=bytes([i])) for i in range(1, 4)]
    for body in bodies:
        await relay.accept_media(RECIPIENT, body)
    reg.online.add(RECIPIENT)
    for _ in range(4):
        await relay.drain(RECIPIENT)
    await relay.settle_media()
    assert [b for _i, b in reg.sent_media] == bodies  # in order, never twice


async def test_an_enqueue_sweeps_expired_media_rows_first(seeded):
    """Expired rows go in the enqueue's own transaction, not only at the
    hourly sweep, so they never hold disk or the server-wide cap."""
    queue = SqliteGfsEnvelopeQueueRepo(seeded)
    for _ in range(3):
        assert await queue.enqueue_media(
            RECIPIENT,
            b"x" * 100,
            created_at=10,
            expires_at=20,
            max_bytes_per_recipient=1000,
            max_total_bytes=300,
        )
    assert await queue.enqueue_media(
        "other2home22222222222222222222ab",
        b"y" * 300,
        created_at=30,
        expires_at=90,
        max_bytes_per_recipient=1000,
        max_total_bytes=300,
    )
    rows = await seeded.fetchall("SELECT COUNT(*) AS n FROM gfs_media_queue")
    assert rows[0]["n"] == 1


class _FlakyMediaRegistry(_Registry):
    """Refuses the first *fail* media pushes (a busy / stalled socket)."""

    def __init__(self, fail: int, **kw) -> None:
        super().__init__(**kw)
        self.fail = fail
        self.attempts: list[float] = []

    async def send_media(self, instance_id, body) -> bool:
        if not self.takes_media(instance_id):
            return False
        self.attempts.append(time.monotonic())
        if self.fail > 0:
            self.fail -= 1
            return False
        return await super().send_media(instance_id, body)


async def test_a_refused_media_push_backs_off_before_the_next_try(seeded, monkeypatch):
    monkeypatch.setattr(envelope_relay_mod, "MEDIA_RETRY_BASE_S", 0.2)
    reg = _FlakyMediaRegistry(1)
    relay = _relay(seeded, reg)
    await relay.accept_media(RECIPIENT, _media())
    reg.online.add(RECIPIENT)
    reg.media.add(RECIPIENT)
    await relay.drain(RECIPIENT)
    await relay.drain(RECIPIENT)  # a re-kick while backing off: no extra push
    await relay.settle_media()
    assert len(reg.attempts) == 2
    assert reg.attempts[1] - reg.attempts[0] >= 0.2
    assert len(reg.sent_media) == 1


async def test_a_socket_that_keeps_refusing_media_is_left_for_the_next_trigger(
    seeded, monkeypatch
):
    monkeypatch.setattr(envelope_relay_mod, "MEDIA_RETRY_BASE_S", 0.01)
    reg = _FlakyMediaRegistry(1000)
    relay = _relay(seeded, reg)
    await relay.accept_media(RECIPIENT, _media())
    reg.online.add(RECIPIENT)
    reg.media.add(RECIPIENT)
    await relay.drain(RECIPIENT)
    await relay.settle_media()
    assert len(reg.attempts) == envelope_relay_mod.MEDIA_RETRY_ATTEMPTS
    queue = SqliteGfsEnvelopeQueueRepo(seeded)
    assert await queue.media_bytes_for(RECIPIENT, now=0) > 0  # still queued
