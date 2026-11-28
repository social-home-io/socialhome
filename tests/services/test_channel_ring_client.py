"""The household side of a GFS space's channel ring (v_97)."""

from __future__ import annotations

from types import SimpleNamespace

import orjson
import pytest

from socialhome.crypto import ed25519_public_key
from socialhome.domain.space import SpaceRole
from socialhome.services import channel_ring_client as mod
from socialhome.services.channel_ring_client import (
    CURSOR_KEY_PREFIX,
    ChannelRingClient,
)
from socialhome.services.gfs_ws_client import RING_CONNECTED_FRAME, RING_TAKEN_FIELD

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
        #: A delegated admin household: delegation on, a local admin seat
        #: and the authority seed the space pins (``serves_as_host``).
        self.delegate = False

    async def spaces_for_gfs_channel(self, channel_id: str) -> list[str]:
        return ["sp-1"] if channel_id == CH else []

    async def list_channel_spaces(self) -> list[str]:
        return ["sp-1"]

    async def get(self, space_id: str):
        return SimpleNamespace(
            id=space_id,
            owner_instance_id=self.owner,
            features=SimpleNamespace(delegated_admin_authority=self.delegate),
            identity_public_key=ed25519_public_key(_SEED).hex(),
        )

    async def list_local_member_user_ids(self, space_id: str) -> list[str]:
        return ["u-admin"]

    async def list_members(self, space_id: str):
        return [SimpleNamespace(user_id="u-admin", role=SpaceRole.ADMIN)]

    async def get_space_seed(self, space_id: str):
        return _SEED if self.delegate else None


_SEED = bytes(range(32))


class _Channels:
    def __init__(self, conns=(GFS, "gfs-b")) -> None:
        self.conns = conns

    async def ring_connections(self, space_id: str):
        if space_id != "sp-1":
            return None
        return CH, [SimpleNamespace(id=c) for c in self.conns]


class _Config:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail = False

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        if self.fail:
            raise RuntimeError("disk")
        self.data[key] = value


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)


class _Sync:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def sync_now(self, space_id: str) -> list[str]:
        self.calls.append(space_id)
        return ["host"]


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


def _item(seq: int, rid: str = RID, *, fetch=None, taken=True, ch=CH) -> dict:
    out = {
        "type": "relay",
        "channel_id": ch,
        "epoch": 1,
        "payload": "p",
        "seq": seq,
        "ring_id": rid,
        RING_TAKEN_FIELD: taken,
    }
    if fetch is not None:
        out["fetch"] = fetch
    return out


def _heads(seq: int, rid: str = RID, ch: str = CH) -> dict:
    return {
        "type": "channel_heads",
        "heads": [{"channel_id": ch, "head_seq": seq, "ring_id": rid}],
    }


def _fetches(w) -> list[dict]:
    return [f for _, f in w.sent if f["type"] == "channel_fetch"]


def _watches(w) -> list[dict]:
    return [f for _, f in w.sent if f["type"] == "channel_watch"]


def _done(w, *, head: int, more: bool = False, req: str | None = None) -> dict:
    return {
        "type": "channel_fetch_done",
        "channel_id": CH,
        "req": req if req is not None else _fetches(w)[-1]["req"],
        "head_seq": head,
        "ring_id": RID,
        "more": more,
    }


def _badges(w) -> list[int]:
    return [e.unread for e in w.bus.events]


async def test_a_new_seat_starts_at_the_head(world):
    await world.client.on_frame(_heads(40), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 40)
    assert _fetches(world) == []
    assert world.bus.events == []


async def test_frames_for_channels_we_do_not_read_there_are_ignored(world):
    """No GFS grows our state or raises badges for a channel it doesn't
    serve us."""
    other = "d" * 32
    await world.client.on_frame(_heads(40, ch=other), gfs_id=GFS)
    await world.client.on_frame(_item(3, ch=other), gfs_id=GFS)
    await world.client.on_frame(_heads(40), gfs_id="gfs-elsewhere")
    assert world.client._cursors == {}
    assert world.bus.events == []


async def test_live_items_in_order_move_the_cursor(world):
    await world.client.on_frame(_item(5), gfs_id=GFS)
    await world.client.on_frame(_item(6), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 6)
    await world.client.on_frame(_item(4), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 6)


async def test_a_dropped_live_item_keeps_the_cursor(world):
    await world.client.on_frame(_item(5), gfs_id=GFS)
    await world.client.on_frame(_item(6, taken=False), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 5)


async def test_a_hole_badges_a_non_viewer_and_never_pulls_a_backlog(world):
    """A live item far past the cursor must not fetch its backlog."""
    await world.client.on_frame(_heads(100), gfs_id=GFS)
    await world.client.on_frame(_item(700), gfs_id=GFS)
    assert _fetches(world) == []
    assert _badges(world) == [1]
    assert world.client.cursor(GFS, CH) == (RID, 100)


async def test_a_hole_is_fetched_where_someone_looks(world):
    await world.client.on_frame(_item(5), gfs_id=GFS)
    await world.client.viewing("sp-1")
    await world.client.on_frame(
        _done(world, head=5, req=_fetches(world)[-1]["req"]), gfs_id=GFS
    )
    await world.client.on_frame(_item(9), gfs_id=GFS)
    (_, fetch) = _fetches(world)
    assert (fetch["since_seq"], fetch["ring_id"]) == (5, RID)


async def test_heads_badge_once_until_caught_up(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    for head in (13, 14, 15):
        await world.client.on_frame(_heads(head), gfs_id=GFS)
    assert _badges(world) == [1]


async def test_the_activity_notice_is_quiet_for_an_hour(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(13), gfs_id=GFS)
    await world.client.viewing("sp-1")
    await world.client.on_frame(_done(world, head=13), gfs_id=GFS)
    assert _badges(world) == [1, 0]
    world.clock.t += mod.WATCH_LINGER_S + 1
    await world.client.on_frame(_heads(20), gfs_id=GFS)
    assert _badges(world) == [1, 0]  # within the quiet hour: no new notice
    world.clock.t += mod.ACTIVITY_QUIET_S
    await world.client.viewing("sp-1")
    await world.client.on_frame(_done(world, head=20), gfs_id=GFS)
    world.clock.t += mod.WATCH_LINGER_S + 1
    await world.client.on_frame(_heads(25), gfs_id=GFS)
    assert _badges(world)[-1] == 1


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


async def test_a_page_moves_over_its_holes_and_more_waits(world):
    """Expired pins leave holes in a page: the page's own items move the
    cursor over them (no livelock), and the next page waits."""
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.viewing("sp-1")
    req = _fetches(world)[-1]["req"]
    for seq in (12, 13, 20):
        await world.client.on_frame(_item(seq, fetch=req), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 20)
    await world.client.on_frame(_done(world, head=900, more=True), gfs_id=GFS)
    assert len(_fetches(world)) == 1
    world.clock.t += mod.PAGE_INTERVAL_S
    await world.client.tick()
    assert _fetches(world)[-1]["since_seq"] == 20


async def test_a_dropped_page_item_stops_that_page(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.viewing("sp-1")
    req = _fetches(world)[-1]["req"]
    await world.client.on_frame(_item(11, fetch=req), gfs_id=GFS)
    await world.client.on_frame(_item(12, fetch=req, taken=False), gfs_id=GFS)
    await world.client.on_frame(_item(13, fetch=req), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 11)
    await world.client.on_frame(_done(world, head=13, more=True), gfs_id=GFS)
    # Final review M5: asked for again after the page interval, not at once.
    assert world.client._next_page == {(GFS, CH): world.clock.t + mod.PAGE_INTERVAL_S}


async def test_an_answer_to_another_request_is_ignored(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.viewing("sp-1")
    await world.client.on_frame(_item(30, fetch="forged"), gfs_id=GFS)
    await world.client.on_frame(_done(world, head=50, req="forged"), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 10)


async def test_the_end_of_a_fetch_moves_to_the_head_and_clears_the_badge(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(20), gfs_id=GFS)
    await world.client.viewing("sp-1")
    await world.client.on_frame(_done(world, head=20), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 20)
    assert _badges(world) == [1, 0]


async def test_the_watch_lingers_then_ends(world):
    await world.client.viewing("sp-1")
    world.clock.t += mod.WATCH_LINGER_S - 1
    await world.client.tick()
    assert all(w["unwatch"] == [] for w in _watches(world))
    world.clock.t += 2
    await world.client.tick()
    assert _watches(world)[-1]["unwatch"] == [CH]


async def test_the_host_always_watches(world):
    world.spaces.owner = OWN
    await world.client.on_frame({"type": RING_CONNECTED_FRAME}, gfs_id=GFS)
    assert _watches(world) == [{"type": "channel_watch", "watch": [CH], "unwatch": []}]
    world.clock.t += 10 * mod.WATCH_LINGER_S
    await world.client.tick()
    assert len([w for w in _watches(world) if w["unwatch"]]) == 0


async def test_every_connect_sends_the_watches_again(world):
    await world.client.viewing("sp-1")
    before = len(_watches(world))
    await world.client.on_frame({"type": RING_CONNECTED_FRAME}, gfs_id=GFS)
    assert len(_watches(world)) == before + 1
    assert _watches(world)[-1]["watch"] == [CH]


@pytest.mark.parametrize("trigger", ["gap", "reset", "too_far"])
async def test_a_gap_skips_ahead_and_runs_the_catch_up(world, trigger):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    if trigger == "gap":
        await world.client.viewing("sp-1")
        req = _fetches(world)[-1]["req"]
        await world.client.on_frame(
            {
                "type": "channel_gap",
                "channel_id": CH,
                "req": req,
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
        top = 10 + mod.MAX_BEHIND_ITEMS + 1
        await world.client.on_frame(_heads(top), gfs_id=GFS)
        expected = (RID, top - mod.RECENT_AFTER_GAP)
    assert world.client.cursor(GFS, CH) == expected
    if trigger != "gap":
        # Final review I1: nobody looks — the catch-up waits for a view.
        assert world.sync.calls == []
        await world.client.viewing("sp-1")
    assert world.sync.calls == ["sp-1"]


async def test_a_gap_answer_needs_our_request(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(
        {
            "type": "channel_gap",
            "channel_id": CH,
            "req": "nope",
            "head_seq": 900,
            "ring_id": RID,
        },
        gfs_id=GFS,
    )
    assert world.sync.calls == []
    assert world.client.cursor(GFS, CH) == (RID, 10)


async def test_the_host_runs_no_catch_up_from_itself(world):
    world.spaces.owner = OWN
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(900, "f" * 16), gfs_id=GFS)
    assert world.sync.calls == []


async def test_gap_catch_ups_are_deferred_not_dropped(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.viewing("sp-1")
    await world.client.on_frame(_heads(900, "1" * 16), gfs_id=GFS)
    await world.client.on_frame(_heads(1900, "2" * 16), gfs_id=GFS)
    assert world.sync.calls == ["sp-1"]
    world.clock.t += mod.GAP_CATCH_UP_INTERVAL_S + 1
    await world.client.tick()
    assert world.sync.calls == ["sp-1"]  # owed: nobody looks now
    await world.client.viewing("sp-1")
    assert world.sync.calls == ["sp-1", "sp-1"]


async def test_failover_starts_a_window_back_on_the_other_gfs(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(5000), gfs_id="gfs-b")
    assert world.client.cursor("gfs-b", CH) == (RID, 5000 - mod.MAX_BEHIND_ITEMS)
    # Nobody looks: a badge, no fetch yet.
    assert _fetches(world) == []
    assert _badges(world) == [1]


async def test_a_fetch_too_far_behind_is_a_gap(world):
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    world.client._heads[(GFS, CH)] = (RID, 10 + mod.MAX_BEHIND_ITEMS + 5)
    await world.client.viewing("sp-1")
    assert world.sync.calls == ["sp-1"]


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


@pytest.mark.parametrize(
    "blob",
    ["not json", "[1]", "null", orjson.dumps({CH: [RID, 2**60]}).decode()],
)
async def test_unreadable_cursors_are_reset(world, blob):
    world.config.data[CURSOR_KEY_PREFIX + GFS] = blob
    await world.client.on_frame(_heads(7), gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 7)


async def test_a_failing_save_is_retried(world):
    await world.client.on_frame(_heads(7), gfs_id=GFS)
    world.config.fail = True
    await world.client.flush()
    world.config.fail = False
    await world.client.flush()
    assert CURSOR_KEY_PREFIX + GFS in world.config.data


async def test_malformed_and_out_of_range_frames_are_dropped(world):
    await world.client.on_frame(_heads(5), gfs_id=GFS)
    for frame in (
        {"type": "channel_heads", "heads": [{"channel_id": "x"}]},
        {"type": "channel_heads", "heads": "x"},
        _heads(2**60),
        {"type": "relay", "channel_id": CH},
        {"type": "relay", "channel_id": "bad", "seq": 1, "ring_id": RID},
    ):
        await world.client.on_frame(frame, gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 5)


async def test_heads_are_bounded_per_frame(world, monkeypatch):
    monkeypatch.setattr(mod, "MAX_WATCHED_CHANNELS", 1)
    frame = {
        "type": "channel_heads",
        "heads": [
            {"channel_id": CH, "head_seq": 3, "ring_id": RID},
            {"channel_id": CH, "head_seq": 9, "ring_id": RID},
        ],
    }
    await world.client.on_frame(frame, gfs_id=GFS)
    assert world.client.cursor(GFS, CH) == (RID, 3)


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


async def test_the_badge_is_readable_for_the_spaces_list(world):
    """``has_activity`` backs ``GET /api/spaces`` ``new_activity``."""
    assert world.client.has_activity("sp-1") is False
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(20), gfs_id=GFS)
    assert world.client.has_activity("sp-1") is True
    await world.client.viewing("sp-1")
    await world.client.on_frame(_done(world, head=20), gfs_id=GFS)
    assert world.client.has_activity("sp-1") is False


async def test_a_delegated_admin_acting_as_host_always_watches(world):
    """Owner reminder 2026-11-28: "the host" is every household acting as
    host (``serves_as_host``) — a delegated admin household watches its
    GFS space always and fetches at each ping, like the owner; it still runs
    the catch-up from its own host above it (only the owner has none)."""
    world.spaces.delegate = True
    await world.client.on_frame({"type": RING_CONNECTED_FRAME}, gfs_id=GFS)
    assert _watches(world) == [{"type": "channel_watch", "watch": [CH], "unwatch": []}]
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    await world.client.on_frame(_heads(12), gfs_id=GFS)
    assert [f["since_seq"] for f in _fetches(world)] == [10]
    await world.client.on_frame(_heads(900, "f" * 16), gfs_id=GFS)
    assert world.sync.calls == ["sp-1"]


async def test_m10_the_host_role_is_read_once_a_minute(world):
    """Fable M10: every watch update reads the host role of every channel
    space — remembered for HOST_ROLE_TTL_S, then read again (a role that
    changed is picked up within a minute)."""
    reads: list[str] = []
    real = world.spaces.get

    async def counting(space_id):
        reads.append(space_id)
        return await real(space_id)

    world.spaces.get = counting
    world.spaces.owner = OWN
    for _ in range(3):
        await world.client.on_frame({"type": RING_CONNECTED_FRAME}, gfs_id=GFS)
    assert len(reads) == 1
    world.spaces.owner = "someone-else"
    world.clock.t += mod.HOST_ROLE_TTL_S
    await world.client.tick()
    assert len(reads) == 2
    assert _watches(world)[-1]["unwatch"] == [CH]


async def test_i1_a_gap_nobody_looks_at_owes_its_catch_up(world):
    """Final review I1: a busy space must not make every idle household ask
    its hosts at each heads ping — a gap where nobody looks is owed, and the
    catch-up runs once someone opens the space."""
    await world.client._load(GFS)
    world.client._set_cursor(GFS, CH, RID, 1)
    await world.client._gap(GFS, CH, "ffffffffffffffff", 5000, 0)
    assert world.sync.calls == []
    await world.client.viewing("sp-1")
    assert world.sync.calls == ["sp-1"]


async def test_m5_a_stalled_page_is_asked_for_again(world):
    """Final review M5: a page the inbound budget cut short is fetched again
    after PAGE_INTERVAL_S, not left until the next item or view."""
    await world.client.on_frame(_heads(10), gfs_id=GFS)
    world.client._heads[(GFS, CH)] = (RID, 50)
    await world.client.viewing("sp-1")
    req = _fetches(world)[-1]["req"]
    world.client._stalled.add((GFS, CH))
    await world.client.on_frame(
        {
            "type": "channel_fetch_done",
            "channel_id": CH,
            "req": req,
            "head_seq": 50,
            "ring_id": RID,
        },
        gfs_id=GFS,
    )
    assert (GFS, CH) in world.client._next_page
