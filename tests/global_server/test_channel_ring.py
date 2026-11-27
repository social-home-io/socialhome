"""The channel ring (v_97, capability ``channel_ring``) — GFS side.

End to end over the real GFS app (``create_gfs_app``) and its real SQLite:
seq allocation and ring ids, live push only to watchers (and every seat for
``push_all``), heads pings, fetch pages, gaps, the seat check on every watch
and fetch, offline copies only for households that do not read the ring,
and the bounds (count, bytes, age, the server cap).
"""

# ruff: noqa: F811 — the ``gfs`` fixture is imported from test_channels.
from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime

import pytest

from socialhome.crypto import b64url_decode, b64url_encode
from socialhome.domain.gfs_channel import (
    CHANNEL_FETCH_DONE_FRAME,
    CHANNEL_GAP_FRAME,
    CHANNEL_HEADS_FRAME,
    RING_CLASS_EPHEMERAL,
    ChannelPublishAnonRequest,
    InvalidChannelWire,
    valid_ring_id,
)
from socialhome.domain.gfs_hello_accepts import ACCEPTS_CHANNEL_RING
from socialhome.gfs_channel import sign_publish_anon
from socialhome.global_server import channel_ring as ring_mod
from socialhome.global_server import channels as channels_mod
from socialhome.global_server.app_keys import (
    gfs_channel_repo_key,
    gfs_channel_ring_key,
    gfs_channel_ring_repo_key,
    gfs_fed_repo_key,
    gfs_ws_registry_key,
)
from tests.global_server.test_channels import (  # noqa: F401 — the fixture
    CT,
    GFS_ID,
    _Channel,
    _queued,
    _ready,
    _seat,
    _wait_connected,
    gfs,
)

pytestmark = pytest.mark.security


def _publish_body(
    ch: _Channel, epoch: int = 3, *, ring_class=None, push_all=False, payload=CT
) -> dict:
    grant = ch.writer(epoch)
    req = ChannelPublishAnonRequest(
        gfs_instance_id=GFS_ID,
        channel_id=ch.id,
        ts=datetime.now(UTC).isoformat(),
        nonce=b64url_encode(os.urandom(16)),
        epoch=epoch,
        payload=payload,
        writer_sig="",
        writer_sig_suite="ed25519",
        ring_class=ring_class,
        push_all=push_all,
    )
    return sign_publish_anon(
        req, writer_seed=b64url_decode(grant.writer_seed)
    ).to_wire()


async def _publish(gfs, **kw) -> None:
    resp = await gfs.post(
        "/gfs/channels/publish-anon", json=_publish_body(gfs.ch, **kw)
    )
    assert resp.status == 200, await resp.text()


def _ring_hello(h) -> dict:
    return {**h.hello(), "accepts": [ACCEPTS_CHANNEL_RING]}


async def _recv(ws, timeout: float = 5.0) -> dict:
    return await asyncio.wait_for(ws.receive_json(), timeout=timeout)


async def _nothing(ws, timeout: float = 0.3) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.receive_json(), timeout=timeout)


# ── Publish: seq, ring id, the signed ring fields ────────────────────────


async def test_every_publish_takes_the_next_seq_in_one_ring(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.other)
    for _ in range(3):
        await _publish(gfs)
    state = await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)
    assert state.head_seq == 3
    assert state.oldest_seq == 1
    assert valid_ring_id(state.ring_id) == state.ring_id
    # A v_96 seat (no ``channel_ring``) still gets its offline copies,
    # now with the ring position (ignored by an older household).
    queued = await _queued(gfs, gfs.other)
    assert [q.sealed["seq"] for q in queued] == [1, 2, 3]
    assert {q.sealed["ring_id"] for q in queued} == {state.ring_id}


async def test_ring_fields_are_signed(gfs) -> None:
    await _ready(gfs)
    body = _publish_body(gfs.ch, ring_class=RING_CLASS_EPHEMERAL)
    body["ring_class"] = "durable"  # tampered after signing
    resp = await gfs.post("/gfs/channels/publish-anon", json=body)
    assert resp.status == 403
    body = _publish_body(gfs.ch)
    body["push_all"] = True  # added after signing
    resp = await gfs.post("/gfs/channels/publish-anon", json=body)
    assert resp.status == 403


async def test_bad_ring_fields_are_refused_at_parse() -> None:
    ch = _Channel()
    for bad in ({"ring_class": "forever"}, {"push_all": False}, {"push_all": 1}):
        body = {**_publish_body(ch), **bad}
        with pytest.raises(InvalidChannelWire):
            ChannelPublishAnonRequest.from_wire(body)


# ── Live push: watchers only, push_all to everyone ───────────────────────


async def test_a_ring_socket_gets_live_items_only_while_watching(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_ring_hello(gfs.member))
        await _wait_connected(gfs.app_, gfs.member.instance_id)
        await _publish(gfs)
        await _nothing(ws)
        await ws.send_json({"type": "channel_watch", "watch": [gfs.ch.id]})
        await asyncio.sleep(0.1)
        await _publish(gfs)
        frame = await _recv(ws)
        assert frame["type"] == "relay" and frame["seq"] == 2
        await ws.send_json({"type": "channel_watch", "unwatch": [gfs.ch.id]})
        await asyncio.sleep(0.1)
        await _publish(gfs)
        await _nothing(ws)
    # Not watching is not "offline": the ring holds it, nothing is queued.
    assert await _queued(gfs, gfs.member) == []


async def test_push_all_reaches_every_seat_and_queues_offline(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _seat(gfs, gfs.other)
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_ring_hello(gfs.member))
        await _wait_connected(gfs.app_, gfs.member.instance_id)
        # Mark ``other`` as a ring household that is offline right now.
        await gfs.app_[gfs_channel_ring_repo_key].set_channel_ring(
            gfs.other.instance_id, True
        )
        await _publish(gfs, push_all=True)
        frame = await _recv(ws)
        assert frame["push_all"] is True and frame["seq"] == 1
    assert len(await _queued(gfs, gfs.other)) == 1


async def test_an_offline_ring_household_gets_no_offline_copy(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.other)
    await gfs.app_[gfs_channel_ring_repo_key].set_channel_ring(
        gfs.other.instance_id, True
    )
    await _publish(gfs)
    assert await _queued(gfs, gfs.other) == []


async def test_watching_needs_a_seat(gfs) -> None:
    await _ready(gfs)
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_ring_hello(gfs.outsider))
        await _wait_connected(gfs.app_, gfs.outsider.instance_id)
        await ws.send_json({"type": "channel_watch", "watch": [gfs.ch.id]})
        await asyncio.sleep(0.1)
        registry = gfs.app_[gfs_ws_registry_key]
        assert registry.watching(gfs.outsider.instance_id) == frozenset()
        await _publish(gfs)
        await _nothing(ws)


# ── Heads ────────────────────────────────────────────────────────────────


async def test_heads_after_hello_and_then_only_when_moved(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _publish(gfs)
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_ring_hello(gfs.member))
        heads = await _recv(ws)
        while heads["type"] != CHANNEL_HEADS_FRAME:
            heads = await _recv(ws)  # its queued copy from before
        ((head,),) = (heads["heads"],)
        assert head["channel_id"] == gfs.ch.id and head["head_seq"] == 1
        ring = gfs.app_[gfs_channel_ring_key]
        assert await ring.send_heads(gfs.member.instance_id, initial=False) == 0
        await _publish(gfs)
        assert await ring.send_heads(gfs.member.instance_id, initial=False) == 1
        moved = await _recv(ws)
        assert moved["heads"][0]["head_seq"] == 2


async def test_a_v96_socket_gets_no_heads_and_its_frames_are_ignored(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _publish(gfs)
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(gfs.member.hello())
        await _wait_connected(gfs.app_, gfs.member.instance_id)
        # Its offline copy of the item (v_96 behaviour), and nothing else.
        queued = await _recv(ws)
        assert queued["type"] == "relay" and "heads" not in queued
        await ws.send_json(
            {
                "type": "channel_fetch",
                "channel_id": gfs.ch.id,
                "since_seq": 0,
                "req": "r",
            }
        )
        await _nothing(ws)


# ── Fetch ────────────────────────────────────────────────────────────────


async def _fetch(ws, ch_id, since, ring_id=None, req="r1") -> list[dict]:
    body = {
        "type": "channel_fetch",
        "channel_id": ch_id,
        "since_seq": since,
        "req": req,
    }
    if ring_id is not None:
        body["ring_id"] = ring_id
    await ws.send_json(body)
    out = []
    while True:
        frame = await _recv(ws)
        if frame["type"] in ("relay", CHANNEL_FETCH_DONE_FRAME, CHANNEL_GAP_FRAME):
            out.append(frame)
        if frame["type"] in (CHANNEL_FETCH_DONE_FRAME, CHANNEL_GAP_FRAME):
            return out


async def _ring_socket(gfs, ws, h) -> None:
    await ws.send_json(_ring_hello(h))
    await _wait_connected(gfs.app_, h.instance_id)
    await asyncio.sleep(0.05)
    # Drain what the hello brings: queued copies from before it read the
    # ring (the drain is never watch-filtered) and the initial heads.
    while True:
        try:
            await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        except TimeoutError:
            return


async def test_fetch_returns_what_followed_the_cursor(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    for _ in range(3):
        await _publish(gfs)
    ring_id = (await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)).ring_id
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        frames = await _fetch(ws, gfs.ch.id, 1, ring_id)
    *items, done = frames
    assert [f["seq"] for f in items] == [2, 3]
    assert all(f["payload"] == CT and f["ring_id"] == ring_id for f in items)
    assert done == {
        "type": CHANNEL_FETCH_DONE_FRAME,
        "channel_id": gfs.ch.id,
        "req": "r1",
        "head_seq": 3,
        "ring_id": ring_id,
        "more": False,
    }


async def test_fetch_pages(gfs, monkeypatch) -> None:
    monkeypatch.setattr(ring_mod, "FETCH_PAGE_MAX_ITEMS", 2)
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    for _ in range(3):
        await _publish(gfs)
    ring_id = (await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)).ring_id
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        *items, done = await _fetch(ws, gfs.ch.id, 0, ring_id)
        assert [f["seq"] for f in items] == [1, 2] and done["more"] is True
        *items, done = await _fetch(ws, gfs.ch.id, 2, ring_id)
        assert [f["seq"] for f in items] == [3] and done["more"] is False


@pytest.mark.parametrize("case", ["ring_id", "ahead", "behind"])
async def test_a_cursor_outside_the_ring_is_a_gap(gfs, monkeypatch, case) -> None:
    monkeypatch.setattr(ring_mod, "RING_MAX_ITEMS", 2)
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    for _ in range(4):  # seq 1, 2 evicted by count
        await _publish(gfs)
    state = await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)
    assert state.oldest_seq == 3
    since, rid = {
        "ring_id": (3, "0" * 16),
        "ahead": (9, state.ring_id),
        "behind": (1, state.ring_id),
    }[case]
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        (gap,) = await _fetch(ws, gfs.ch.id, since, rid)
    assert gap == {
        "type": CHANNEL_GAP_FRAME,
        "channel_id": gfs.ch.id,
        "req": "r1",
        "head_seq": 4,
        "ring_id": state.ring_id,
        "oldest_seq": 3,
    }


async def test_a_cursor_right_before_the_oldest_is_no_gap(gfs, monkeypatch) -> None:
    monkeypatch.setattr(ring_mod, "RING_MAX_ITEMS", 2)
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    for _ in range(4):
        await _publish(gfs)
    state = await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        *items, done = await _fetch(ws, gfs.ch.id, 2, state.ring_id)
    assert [f["seq"] for f in items] == [3, 4]
    assert done["type"] == CHANNEL_FETCH_DONE_FRAME


async def test_fetching_needs_a_seat(gfs) -> None:
    await _ready(gfs)
    await _publish(gfs)
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.outsider)
        await ws.send_json(
            {
                "type": "channel_fetch",
                "channel_id": gfs.ch.id,
                "since_seq": 0,
                "req": "r",
            }
        )
        await _nothing(ws)


async def test_fetches_are_rate_limited_per_socket(gfs, monkeypatch) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    ring = gfs.app_[gfs_channel_ring_key]
    ring._fetch_limiter._limit = 1
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        await _fetch(ws, gfs.ch.id, 0)
        await ws.send_json(
            {
                "type": "channel_fetch",
                "channel_id": gfs.ch.id,
                "since_seq": 0,
                "req": "r",
            }
        )
        await _nothing(ws)


async def test_a_new_registration_starts_a_new_ring(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _publish(gfs)
    repo = gfs.app_[gfs_channel_ring_repo_key]
    first = (await repo.state(gfs.ch.id)).ring_id
    # Unregister (or the idle sweep) drops the channel and, by cascade,
    # its ring.
    await gfs.app_[gfs_channel_repo_key].delete(gfs.ch.id)
    assert await repo.state(gfs.ch.id) is None
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _publish(gfs)
    second = await repo.state(gfs.ch.id)
    assert second.head_seq == 1 and second.ring_id != first


# ── Bounds ───────────────────────────────────────────────────────────────


async def test_the_ring_is_bounded_by_bytes(gfs, monkeypatch) -> None:
    monkeypatch.setattr(ring_mod, "RING_MAX_BYTES", 3 * len(CT))
    await _ready(gfs)
    for _ in range(5):
        await _publish(gfs)
    state = await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)
    assert (state.oldest_seq, state.head_seq) == (3, 5)
    assert await gfs.app_[gfs_channel_ring_repo_key].total_bytes() == 3 * len(CT)


async def test_ephemeral_items_expire_first(gfs) -> None:
    await _ready(gfs)
    await _publish(gfs, ring_class=RING_CLASS_EPHEMERAL)
    await _publish(gfs)
    ring = gfs.app_[gfs_channel_ring_key]
    base = time.time()
    ring._clock = lambda: base + 16 * 60
    assert await ring.sweep() == 1
    state = await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)
    assert (state.oldest_seq, state.head_seq) == (2, 2)
    ring._clock = lambda: base + 25 * 3600
    assert await ring.sweep() == 1
    assert await gfs.app_[gfs_channel_ring_repo_key].total_bytes() == 0


async def test_the_server_cap_trims_the_biggest_ring_first(gfs, monkeypatch) -> None:
    await _ready(gfs)
    big = gfs.ch
    for _ in range(4):
        await _publish(gfs)
    small = _Channel()
    gfs.ch = small
    await _ready(gfs)
    await _publish(gfs)
    monkeypatch.setattr(ring_mod, "SERVER_MAX_BYTES", 4 * len(CT))
    await gfs.app_[gfs_channel_ring_key].sweep()
    repo = gfs.app_[gfs_channel_ring_repo_key]
    assert (await repo.state(big.id)).oldest_seq == 2
    assert (await repo.state(small.id)).oldest_seq == 1
    assert await repo.total_bytes() <= 4 * len(CT)


async def test_the_ring_loop_stops_cleanly(gfs) -> None:
    ring = gfs.app_[gfs_channel_ring_key]
    await ring.start()
    await ring.stop()
    await ring.stop()


# ── Review fixes ─────────────────────────────────────────────────────────


async def test_an_expired_tail_ends_the_fetch(gfs) -> None:
    """The newest item was ephemeral and expired: the page is empty, the
    answer says ``more: false`` (move the cursor to the head) — never a
    fetch loop."""
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _publish(gfs)
    await _publish(gfs, ring_class=RING_CLASS_EPHEMERAL)
    ring = gfs.app_[gfs_channel_ring_key]
    base = time.time()
    ring._clock = lambda: base + 16 * 60
    await ring.sweep()
    ring._clock = time.time
    rid = (await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)).ring_id
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        (done,) = await _fetch(ws, gfs.ch.id, 1, rid)
    assert done["type"] == CHANNEL_FETCH_DONE_FRAME
    assert (done["more"], done["head_seq"]) == (False, 2)


async def test_two_copies_of_one_publish_are_stored_once(gfs) -> None:
    await _ready(gfs)
    body = _publish_body(gfs.ch)
    first, second = await asyncio.gather(
        gfs.post("/gfs/channels/publish-anon", json=body),
        gfs.post("/gfs/channels/publish-anon", json=body),
    )
    assert sorted([first.status, second.status]) == [200, 403]
    state = await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)
    assert state.head_seq == 1


async def test_push_all_has_its_own_small_budget(gfs) -> None:
    await _ready(gfs)
    for _ in range(channels_mod.CHANNEL_PUSH_ALL_MAX_PER_MINUTE):
        await _publish(gfs, push_all=True)
    resp = await gfs.post(
        "/gfs/channels/publish-anon", json=_publish_body(gfs.ch, push_all=True)
    )
    assert resp.status == 429
    # Ordinary items are not charged to it.
    await _publish(gfs)


async def test_watch_changes_are_rate_limited(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    ring = gfs.app_[gfs_channel_ring_key]
    ring._watch_limiter._limit = 1
    registry = gfs.app_[gfs_ws_registry_key]
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        await ws.send_json({"type": "channel_watch", "unwatch": [gfs.ch.id]})
        await ws.send_json({"type": "channel_watch", "watch": [gfs.ch.id]})
        await asyncio.sleep(0.2)
        assert registry.watching(gfs.member.instance_id) == frozenset()


async def test_the_ring_is_bounded_by_count(gfs, monkeypatch) -> None:
    monkeypatch.setattr(ring_mod, "RING_MAX_ITEMS", 3)
    await _ready(gfs)
    for _ in range(5):
        await _publish(gfs)
    state = await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)
    assert (state.oldest_seq, state.head_seq) == (3, 5)
    assert await gfs.app_[gfs_channel_ring_repo_key].total_bytes() == 3 * len(CT)


async def test_the_server_cap_cuts_the_biggest_rings_to_one_level(gfs) -> None:
    """Fair eviction: the cut is shared — the biggest rings come down to a
    common level, a ring below it is untouched, never one ring emptied."""
    await _ready(gfs)
    big = gfs.ch
    for _ in range(6):
        await _publish(gfs)
    mid = _Channel()
    gfs.ch = mid
    await _ready(gfs)
    for _ in range(4):
        await _publish(gfs)
    small = _Channel()
    gfs.ch = small
    await _ready(gfs)
    await _publish(gfs)
    repo = gfs.app_[gfs_channel_ring_repo_key]
    # 11 items; cap 7 → 4 must go: big 6 → 3, mid 4 → 3, small untouched.
    await repo.trim_to_cap(cap=7 * len(CT))
    assert (await repo.state(big.id)).oldest_seq == 4
    assert (await repo.state(mid.id)).oldest_seq == 2
    assert (await repo.state(small.id)).oldest_seq == 1
    assert await repo.total_bytes() <= 7 * len(CT)


async def test_an_expired_ephemeral_item_is_no_gap(gfs) -> None:
    """A cursor behind an expired location pin missed nothing durable."""
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _publish(gfs)
    await _publish(gfs, ring_class=RING_CLASS_EPHEMERAL)
    await _publish(gfs)
    ring = gfs.app_[gfs_channel_ring_key]
    base = time.time()
    ring._clock = lambda: base + 16 * 60
    await ring.sweep()
    ring._clock = time.time
    rid = (await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)).ring_id
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        *items, done = await _fetch(ws, gfs.ch.id, 1, rid)
    assert [f["seq"] for f in items] == [3]
    assert done["type"] == CHANNEL_FETCH_DONE_FRAME


async def test_an_expired_durable_item_is_a_gap(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _publish(gfs)
    ring = gfs.app_[gfs_channel_ring_key]
    base = time.time()
    ring._clock = lambda: base + 25 * 3600
    await ring.sweep()
    ring._clock = time.time
    await _publish(gfs)
    rid = (await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)).ring_id
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        (gap,) = await _fetch(ws, gfs.ch.id, 0, rid)
    assert gap["type"] == CHANNEL_GAP_FRAME


async def test_a_banned_household_neither_fetches_nor_hears_heads(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _publish(gfs)
    rid = (await gfs.app_[gfs_channel_ring_repo_key].state(gfs.ch.id)).ring_id
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        await gfs.app_[gfs_fed_repo_key].set_instance_status(
            gfs.member.instance_id, "banned"
        )
        ring = gfs.app_[gfs_channel_ring_key]
        await _publish(gfs)
        assert await ring.send_heads(gfs.member.instance_id, initial=True) == 0
        await ws.send_json(
            {
                "type": "channel_fetch",
                "channel_id": gfs.ch.id,
                "since_seq": 0,
                "ring_id": rid,
                "req": "r",
            }
        )
        await _nothing(ws)


async def test_queued_copies_reach_a_household_that_now_reads_the_ring(gfs) -> None:
    """Copies queued while it was a v_96 household are delivered on its
    ring hello (the drain is never watch-filtered)."""
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _publish(gfs)
    assert len(await _queued(gfs, gfs.member)) == 1
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_ring_hello(gfs.member))
        got = [await _recv(ws), await _recv(ws)]
    assert {f["type"] for f in got} == {"relay", CHANNEL_HEADS_FRAME}
    assert await _queued(gfs, gfs.member) == []


async def test_a_ring_frame_error_never_closes_the_socket(gfs, monkeypatch) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ring_mod.GfsChannelRing, "_fetch", boom)
    async with gfs.ws_connect("/gfs/ws") as ws:
        await _ring_socket(gfs, ws, gfs.member)
        await ws.send_json(
            {
                "type": "channel_fetch",
                "channel_id": gfs.ch.id,
                "since_seq": 0,
                "req": "r",
            }
        )
        await asyncio.sleep(0.2)
        assert not ws.closed
