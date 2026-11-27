"""The household side of a GFS space's channel ring (v_97)."""

from __future__ import annotations

from types import SimpleNamespace

import orjson
import pytest

from socialhome.domain.events import SpaceChannelActivity
from socialhome.services import channel_ring_client as mod
from socialhome.services.channel_ring_client import (
    CURSOR_KEY_PREFIX,
    ChannelRingClient,
)
from socialhome.services.gfs_ws_client import RING_CONNECTED_FRAME

CH = "c" * 32
RID = "0123456789abcdef"
GFS = "gfs-a"
OWN = "own-household"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Spaces:
    def __init__(self) -> None:
        self.owner = "host-household"

    async def spaces_for_gfs_channel(self, channel_id: str) -> list[str]:
        return ["sp-1"] if channel_id == CH else []

    async def get(self, space_id: str):
        return SimpleNamespace(id=space_id, owner_instance_id=self.owner)

    async def hosted_channel_spaces(self, owner: str) -> list[str]:
        return ["sp-1"] if self.owner == owner else []


class _Channels:
    def __init__(self, conns=(GFS,)) -> None:
        self.conns = conns

    async def ring_connections(self, space_id: str):
        if space_id != "sp-1":
            return None
        return CH, [SimpleNamespace(id=c) for c in self.conns]


class _Config:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)


class _Sync:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue_sync_for_space(self, **kw) -> None:
        self.calls.append(kw)


@pytest.fixture
def world():
    sent: list[tuple[str, dict]] = []

    async def send(gfs_id: str, frame: dict) -> bool:
        sent.append((gfs_id, frame))
        return True

    spaces, config, bus, sync, clock = _Spaces(), _Config(), _Bus(), _Sync(), _Clock()
    client = ChannelRingClient(
        spaces=spaces,  # type: ignore[arg-type]
        channels=_Channels(),
        config=config,  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        own_instance_id=OWN,
        clock=clock,
    )
    client.attach_sender(send)
    client.attach_sync_scheduler(sync)
    return SimpleNamespace(
        client=client,
        sent=sent,
        spaces=spaces,
        config=config,
        bus=bus,
        sync=sync,
        clock=clock,
    )


def _item(seq: int, rid: str = RID) -> dict:
    return {
        "type": "relay",
        "channel_id": CH,
        "epoch": 1,
        "payload": "p",
        "seq": seq,
        "ring_id": rid,
    }


def _heads(seq: int, rid: str = RID) -> dict:
    return {
        "type": "channel_heads",
        "heads": [{"channel_id": CH, "head_seq": seq, "ring_id": rid}],
    }


def _fetches(w) -> list[dict]:
    return [f for _, f in w.sent if f["type"] == "channel_fetch"]


def _watches(w) -> list[dict]:
    return [f for _, f in w.sent if f["type"] == "channel_watch"]


async def test_a_new_seat_starts_at_the_head(world):
    await world.client.on_frame(_heads(40), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 40)
    assert _fetches(world) == []
    assert world.bus.events == []


async def test_items_in_order_move_the_cursor(world):
    await world.client.on_frame(_item(5), gfs_id=GFS)
    await world.client.on_frame(_item(6), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 6)
    # An older copy (a fetch overlapping a live push) changes nothing.
    await world.client.on_frame(_item(4), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 6)


async def test_a_hole_before_a_live_item_is_fetched(world):
    await world.client.on_frame(_item(5), gfs_id=GFS)
    await world.client.on_frame(_item(9), gfs_id=GFS)
    (fetch,) = _fetches(world)
    assert (fetch["since_seq"], fetch["ring_id"]) == (5, RID)
    # One fetch in flight at a time.
    await world.client.on_frame(_item(10), gfs_id=GFS)
    assert len(_fetches(world)) == 1


async def test_heads_above_the_cursor_raise_the_badge(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(13), gfs_id=GFS)
    assert world.bus.events == [
        SpaceChannelActivity(
            space_id="sp-1", unread=3, occurred_at=world.bus.events[0].occurred_at
        )
    ]
    assert _fetches(world) == []
    # The same count again is not re-announced.
    await world.client.on_frame(_heads(13), gfs_id=GFS)
    assert len(world.bus.events) == 1


async def test_the_host_fetches_at_each_ping(world):
    world.spaces.owner = OWN
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(12), gfs_id=GFS)
    assert [f["since_seq"] for f in _fetches(world)] == [10]
    assert world.bus.events == []


async def test_opening_a_space_watches_and_pulls(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.viewing("sp-1")
    assert _watches(world)[-1] == {
        "type": "channel_watch",
        "watch": [CH],
        "unwatch": [],
    }
    assert [f["since_seq"] for f in _fetches(world)] == [10]
    # A viewed space fetches at a ping instead of badging.
    await world.client.on_frame(
        {
            "type": "channel_fetch_done",
            "channel_id": CH,
            "req": "r",
            "head_seq": 10,
            "ring_id": RID,
            "more": False,
        },
        gfs_id=GFS,
    )
    await world.client.on_frame(_heads(11), gfs_id=GFS)
    assert len(_fetches(world)) == 2


async def test_the_watch_lingers_then_ends(world):
    await world.client.viewing("sp-1")
    world.clock.t += mod.WATCH_LINGER_S - 1
    await world.client.tick()
    assert len(_watches(world)) == 1
    world.clock.t += 2
    await world.client.tick()
    assert _watches(world)[-1] == {
        "type": "channel_watch",
        "watch": [],
        "unwatch": [CH],
    }


async def test_the_host_always_watches(world):
    world.spaces.owner = OWN
    await world.client.on_frame({"type": RING_CONNECTED_FRAME}, gfs_id=GFS)
    assert _watches(world) == [{"type": "channel_watch", "watch": [CH], "unwatch": []}]
    world.clock.t += 10 * mod.WATCH_LINGER_S
    await world.client.tick()
    assert len(_watches(world)) == 1


async def test_every_connect_sends_the_watches_again(world):
    await world.client.viewing("sp-1")
    await world.client.on_frame({"type": RING_CONNECTED_FRAME}, gfs_id=GFS)
    assert len(_watches(world)) == 2
    assert _watches(world)[-1]["watch"] == [CH]


async def test_more_pages_are_fetched_and_the_end_moves_to_the_head(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.viewing("sp-1")
    done = {
        "type": "channel_fetch_done",
        "channel_id": CH,
        "req": "r",
        "head_seq": 20,
        "ring_id": RID,
        "more": True,
    }
    await world.client.on_frame(done, gfs_id=GFS)
    assert len(_fetches(world)) == 2
    # The tail expired (an ephemeral item): done without more → the head.
    await world.client.on_frame({**done, "more": False}, gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 20)
    assert world.bus.events[-1].unread == 0 if world.bus.events else True


@pytest.mark.parametrize("trigger", ["gap", "reset", "too_far"])
async def test_a_gap_skips_ahead_and_runs_the_catch_up(world, trigger):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    if trigger == "gap":
        await world.client.on_frame(
            {
                "type": "channel_gap",
                "channel_id": CH,
                "req": "r",
                "head_seq": 900,
                "ring_id": RID,
                "oldest_seq": 850,
            },
            gfs_id=GFS,
        )
        expected = (RID, 849)
    elif trigger == "reset":
        await world.client.on_frame(_heads(900, "f" * 16), gfs_id=GFS)
        expected = ("f" * 16, 900 - mod.RECENT_AFTER_GAP)
    else:
        await world.client.on_frame(_heads(10 + mod.MAX_BEHIND_ITEMS + 1), gfs_id=GFS)
        expected = (RID, 10 + mod.MAX_BEHIND_ITEMS + 1 - mod.RECENT_AFTER_GAP)
    assert world.client.cursor(GFS, CH) == expected
    assert _fetches(world)[-1]["since_seq"] == expected[1]
    assert world.sync.calls == [
        {"space_id": "sp-1", "peer_instance_id": "host-household"}
    ]


async def test_the_host_runs_no_catch_up_from_itself(world):
    world.spaces.owner = OWN
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(900, "f" * 16), gfs_id=GFS)
    assert world.sync.calls == []


async def test_failover_starts_a_window_back_on_the_other_gfs(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(5000), gfs_id="gfs-b")
    assert world.client.cursor("gfs-b", CH) == (RID, 5000 - mod.MAX_BEHIND_ITEMS)
    assert _fetches(world)[-1]["since_seq"] == 5000 - mod.MAX_BEHIND_ITEMS


async def test_cursors_survive_a_restart(world):
    await world.client.on_frame(_heads(42), gfs_id=GFS)
    await world.client.flush()
    raw = world.config.data[CURSOR_KEY_PREFIX + GFS]
    assert orjson.loads(raw) == {CH: [RID, 42]}
    fresh = ChannelRingClient(
        spaces=world.spaces,
        channels=_Channels(),
        config=world.config,
        bus=world.bus,
        own_instance_id=OWN,
    )
    await fresh.on_frame(_heads(43), gfs_id=GFS)
    assert fresh.cursor(GFS, CH) == (RID, 42)


async def test_unreadable_cursors_are_reset(world):
    world.config.data[CURSOR_KEY_PREFIX + GFS] = "not json"
    await world.client.on_frame(_heads(7), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 7)


async def test_malformed_frames_are_dropped(world):
    for frame in (
        {"type": "channel_heads", "heads": [{"channel_id": "x"}]},
        {"type": "channel_heads", "heads": "x"},
        {"type": "channel_gap", "channel_id": CH},
        {"type": "relay", "channel_id": CH},
        {"type": "relay", "channel_id": "bad", "seq": 1, "ring_id": RID},
    ):
        await world.client.on_frame(frame, gfs_id=GFS)
    assert world.client.cursor(GFS, CH) is None


async def test_without_a_sender_nothing_is_sent(world):
    world.client._send = None
    await world.client.viewing("sp-1")
    assert world.sent == []


async def test_a_space_without_a_channel_is_not_watched(world):
    await world.client.viewing("sp-other")
    assert world.sent == []


async def test_the_loop_starts_and_stops(world):
    await world.client.start()
    await world.client.stop()
