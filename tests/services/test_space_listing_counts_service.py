"""Tests for :mod:`socialhome.services.space_listing_counts_service`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from socialhome.domain.events import (
    SpaceConfigChanged,
    SpaceMemberJoined,
    SpaceMemberLeft,
    SpaceRemoteSeatEnded,
    SpaceRemoteSeatLive,
)
from socialhome.domain.public_space import PublicSpaceListing
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.space_listing_counts import SpaceListingCounts
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.peer_space_directory_repo import PeerSpaceDirectoryEntry
from socialhome.services.space_listing_counts_service import (
    SpaceListingCountsService,
)

OWN = "own-inst"


def _space(space_id: str, tier: SpaceType, *, owner: str = OWN) -> Space:
    return Space(
        id=space_id,
        name=space_id,
        owner_instance_id=owner,
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(allow_subscribers=True),
        space_type=tier,
        join_mode=JoinMode.OPEN,
    )


@dataclass
class _SpaceRepo:
    spaces: dict[str, Space] = field(default_factory=dict)
    counts: dict[str, SpaceListingCounts] = field(default_factory=dict)
    roster: set[str] = field(default_factory=set)

    async def get(self, space_id: str) -> Space | None:
        return self.spaces.get(space_id)

    async def listing_counts(self, space_id: str) -> SpaceListingCounts | None:
        return self.counts.get(space_id)

    async def holds_roster(self, space_id: str) -> bool:
        return space_id in self.roster


@dataclass
class _PublicSpaceRepo:
    rows: dict[str, PublicSpaceListing] = field(default_factory=dict)

    async def get(self, space_id: str) -> PublicSpaceListing | None:
        return self.rows.get(space_id)


@dataclass
class _PeerDirectoryRepo:
    rows: dict[tuple[str, str], PeerSpaceDirectoryEntry] = field(default_factory=dict)

    async def get(self, instance_id: str, space_id: str):
        return self.rows.get((instance_id, space_id))


@dataclass
class _Gfs:
    calls: list[tuple[str, bool]] = field(default_factory=list)
    accepted: int = 1

    async def republish_space(self, space_id: str, *, announce: bool = True) -> int:
        self.calls.append((space_id, announce))
        return self.accepted


@dataclass
class _PeerDirectory:
    refreshes: int = 0

    async def refresh(self) -> None:
        self.refreshes += 1


@pytest.fixture
def env():
    bus = EventBus()
    spaces = _SpaceRepo()
    public = _PublicSpaceRepo()
    peers = _PeerDirectoryRepo()
    gfs = _Gfs()
    peer_dir = _PeerDirectory()
    svc = SpaceListingCountsService(
        bus=bus,
        space_repo=spaces,  # type: ignore[arg-type]
        public_space_repo=public,  # type: ignore[arg-type]
        peer_space_directory_repo=peers,  # type: ignore[arg-type]
        gfs=gfs,
        debounce_s=0.05,
    )
    svc.attach(peer_directory=peer_dir, own_instance_id=OWN)
    svc.wire()
    return bus, spaces, public, peers, gfs, peer_dir, svc


async def _settle(svc: SpaceListingCountsService) -> None:
    while svc._tasks:
        await asyncio.gather(*svc._tasks.values())


# ── Re-sharing ────────────────────────────────────────────────────────


async def test_a_burst_of_seat_changes_is_one_gfs_republish(env):
    bus, spaces, _public, _peers, gfs, peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=3, follower_count=1)
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u1"))
    await bus.publish(SpaceMemberLeft(space_id="g", user_id="u2"))
    await bus.publish(SpaceRemoteSeatLive(space_id="g", instance_id="p", user_id="r"))
    await bus.publish(SpaceRemoteSeatEnded(space_id="g", instance_id="p", user_id="r"))
    assert len(svc._tasks) == 1
    await _settle(svc)
    # One re-publish, to the servers already listing it, no re-announce.
    assert gfs.calls == [("g", False)]
    assert peer_dir.refreshes == 0


async def test_a_public_space_refreshes_the_paired_directory(env):
    bus, spaces, _public, _peers, gfs, peer_dir, svc = env
    spaces.spaces["p"] = _space("p", SpaceType.PUBLIC)
    spaces.counts["p"] = SpaceListingCounts(member_count=2, follower_count=0)
    await bus.publish(SpaceMemberJoined(space_id="p", user_id="u1"))
    await _settle(svc)
    assert (gfs.calls, peer_dir.refreshes) == ([], 1)


async def test_unchanged_counts_send_nothing(env):
    bus, spaces, _public, _peers, gfs, _peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=3, follower_count=1)
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u1"))
    await _settle(svc)
    # A join and a leave inside the next window: the same counts.
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u2"))
    await bus.publish(SpaceMemberLeft(space_id="g", user_id="u2"))
    await _settle(svc)
    assert len(gfs.calls) == 1
    spaces.counts["g"] = SpaceListingCounts(member_count=4, follower_count=1)
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u3"))
    await _settle(svc)
    assert len(gfs.calls) == 2


@pytest.mark.parametrize("tier", [SpaceType.GLOBAL, SpaceType.PUBLIC])
async def test_switching_followers_off_and_on_reshares_the_counts(env, tier):
    """The followers switch is a config change; the builder then declares no
    follower count (off) or one again (on), and each flip is re-shared."""
    bus, spaces, _public, _peers, gfs, peer_dir, svc = env
    spaces.spaces["s"] = _space("s", tier)
    spaces.counts["s"] = SpaceListingCounts(member_count=3, follower_count=2)
    await bus.publish(SpaceMemberJoined(space_id="s", user_id="u1"))
    await _settle(svc)
    for followers in (None, 0):
        spaces.counts["s"] = SpaceListingCounts(
            member_count=3, follower_count=followers
        )
        await bus.publish(
            SpaceConfigChanged(
                space_id="s", event_type="feature_changed", payload={}, sequence=1
            )
        )
        await _settle(svc)
    assert len(gfs.calls) + peer_dir.refreshes == 3
    assert svc._last["s"] == SpaceListingCounts(member_count=3, follower_count=0)


async def test_a_republish_nobody_took_is_tried_on_the_next_change(env):
    bus, spaces, _public, _peers, gfs, _peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=3, follower_count=1)
    gfs.accepted = 0
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u1"))
    await _settle(svc)
    gfs.accepted = 1
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u1"))
    await _settle(svc)
    assert len(gfs.calls) == 2


@pytest.mark.parametrize(
    "space",
    [
        _space("x", SpaceType.PRIVATE),
        _space("x", SpaceType.HOUSEHOLD),
        _space("x", SpaceType.GLOBAL, owner="someone-else"),
    ],
)
async def test_only_a_listed_space_hosted_here_is_reshared(env, space):
    bus, spaces, _public, _peers, gfs, peer_dir, svc = env
    spaces.spaces["x"] = space
    spaces.counts["x"] = SpaceListingCounts(member_count=1, follower_count=0)
    await bus.publish(SpaceMemberJoined(space_id="x", user_id="u1"))
    await _settle(svc)
    assert (gfs.calls, peer_dir.refreshes, svc._last) == ([], 0, {})


async def test_concurrent_events_are_one_timer_and_one_republish(env):
    """REGRESSION (review I1): the slot is taken before any await, so events
    racing in through a slow repo read still make one timer, one publish."""
    _bus, spaces, _public, _peers, gfs, _peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=3, follower_count=1)
    real_get = spaces.get

    async def _slow_get(space_id: str):
        await asyncio.sleep(0.01)
        return await real_get(space_id)

    spaces.get = _slow_get  # type: ignore[method-assign]
    await asyncio.gather(
        *(
            svc._on_seat_changed(SpaceMemberJoined(space_id="g", user_id=f"u{i}"))
            for i in range(20)
        )
    )
    assert len(svc._tasks) == 1
    await _settle(svc)
    assert gfs.calls == [("g", False)]


async def test_two_flushes_never_send_side_by_side(env):
    _bus, spaces, _public, _peers, gfs, _peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=3, follower_count=1)
    await asyncio.gather(svc.flush("g"), svc.flush("g"))
    assert len(gfs.calls) == 1


async def test_nothing_is_marked_after_stop(env):
    bus, spaces, *_rest, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    await svc.stop()
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u1"))
    assert svc._tasks == {}


async def test_the_host_shows_its_roster_counts_without_a_member_seat(env):
    """REGRESSION (review M4): the host holds the roster even when none of
    its people holds a member seat."""
    _bus, spaces, *_rest, svc = env
    spaces.counts["g"] = SpaceListingCounts(member_count=2, follower_count=7)
    assert await svc.shown_counts(_space("g", SpaceType.GLOBAL)) == (2, 7)


async def test_nothing_is_marked_before_federation_is_up(env):
    _bus, spaces, _public, _peers, _gfs, _peer_dir, _svc = env
    svc = SpaceListingCountsService(
        bus=EventBus(),
        space_repo=spaces,  # type: ignore[arg-type]
        public_space_repo=_public,  # type: ignore[arg-type]
        peer_space_directory_repo=_peers,  # type: ignore[arg-type]
        gfs=_gfs,
    )
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    await svc._on_seat_changed(SpaceMemberJoined(space_id="g", user_id="u"))
    assert svc._tasks == {}


async def test_stop_drops_pending_timers(env):
    bus, spaces, _public, _peers, gfs, _peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=3, follower_count=1)
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u1"))
    await svc.stop()
    await asyncio.sleep(0.1)
    assert (svc._tasks, gfs.calls) == ({}, [])


async def test_a_failing_republish_never_kills_the_timer(env, caplog):
    bus, spaces, _public, _peers, gfs, _peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=3, follower_count=1)

    async def _boom(space_id: str, *, announce: bool = True) -> int:
        raise RuntimeError("down")

    gfs.republish_space = _boom  # type: ignore[method-assign]
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u1"))
    await _settle(svc)
    assert "re-sharing its listing counts failed" in caplog.text
    assert svc._last == {}


# ── Showing ───────────────────────────────────────────────────────────


async def test_the_roster_counts_when_this_household_holds_it(env):
    _bus, spaces, public, *_rest, svc = env
    space = _space("g", SpaceType.GLOBAL, owner="host")
    spaces.roster.add("g")
    spaces.counts["g"] = SpaceListingCounts(member_count=8, follower_count=5)
    public.rows["g"] = PublicSpaceListing(
        space_id="g", instance_id="host", name="g", member_count=1, follower_count=1
    )
    assert await svc.shown_counts(space) == (8, 5)


async def test_a_follower_household_shows_the_shared_listing(env):
    _bus, spaces, public, peers, *_rest, svc = env
    g = _space("g", SpaceType.GLOBAL, owner="host")
    spaces.counts["g"] = SpaceListingCounts(member_count=0, follower_count=1)
    public.rows["g"] = PublicSpaceListing(
        space_id="g", instance_id="host", name="g", member_count=12, follower_count=30
    )
    assert await svc.shown_counts(g) == (12, 30)
    p = _space("p", SpaceType.PUBLIC, owner="host")
    peers.rows[("host", "p")] = PeerSpaceDirectoryEntry(
        instance_id="host", space_id="p", name="p", member_count=4
    )
    # An older paired host shares no follower count: no number.
    assert await svc.shown_counts(p) == (4, None)


async def test_no_shared_listing_shows_no_number(env):
    *_rest, svc = env
    assert await svc.shown_counts(_space("g", SpaceType.GLOBAL, owner="h")) == (
        None,
        None,
    )
    assert await svc.shown_counts(_space("p", SpaceType.PUBLIC, owner="h")) == (
        None,
        None,
    )


async def test_an_older_gfs_listing_shows_no_number(env):
    _bus, _spaces, public, *_rest, svc = env
    public.rows["g"] = PublicSpaceListing(space_id="g", instance_id="h", name="g")
    assert await svc.shown_counts(_space("g", SpaceType.GLOBAL, owner="h")) == (
        None,
        None,
    )


async def test_an_unlisted_tier_always_reads_the_roster(env):
    _bus, spaces, *_rest, svc = env
    spaces.counts["x"] = SpaceListingCounts(member_count=2, follower_count=0)
    assert await svc.shown_counts(_space("x", SpaceType.PRIVATE)) == (2, 0)
    assert await svc.shown_counts(_space("y", SpaceType.PRIVATE)) == (None, None)


async def test_a_ban_or_role_change_is_a_config_change_that_marks_it(env):
    bus, spaces, _public, _peers, gfs, _peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=2, follower_count=0)
    await bus.publish(
        SpaceConfigChanged(
            space_id="g", event_type="SPACE_MEMBER_BANNED", payload={}, sequence=3
        )
    )
    await _settle(svc)
    assert gfs.calls == [("g", False)]


async def test_stop_cancels_a_republish_in_flight(env):
    """REGRESSION (re-review N1): the timer stays tracked through its
    re-share, so ``stop`` cancels a publish still running."""
    bus, spaces, _public, _peers, gfs, _peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=3, follower_count=1)
    started = asyncio.Event()

    async def _hang(space_id: str, *, announce: bool = True) -> int:
        started.set()
        await asyncio.sleep(10)
        return 1

    gfs.republish_space = _hang  # type: ignore[method-assign]
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u1"))
    await asyncio.wait_for(started.wait(), 1)
    assert "g" in svc._tasks
    await asyncio.wait_for(svc.stop(), 1)
    assert svc._tasks == {}


async def test_a_change_during_a_reshare_rearms_the_timer(env):
    bus, spaces, _public, _peers, gfs, _peer_dir, svc = env
    spaces.spaces["g"] = _space("g", SpaceType.GLOBAL)
    spaces.counts["g"] = SpaceListingCounts(member_count=3, follower_count=1)
    started, release = asyncio.Event(), asyncio.Event()

    async def _slow(space_id: str, *, announce: bool = True) -> int:
        gfs.calls.append((space_id, announce))
        started.set()
        await release.wait()
        return 1

    gfs.republish_space = _slow  # type: ignore[method-assign]
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u1"))
    await asyncio.wait_for(started.wait(), 1)
    # The counts move while the first re-share is on the wire.
    spaces.counts["g"] = SpaceListingCounts(member_count=4, follower_count=1)
    await bus.publish(SpaceMemberJoined(space_id="g", user_id="u2"))
    release.set()
    await asyncio.sleep(0)
    await _settle(svc)
    assert len(gfs.calls) == 2
    assert svc._last["g"] == SpaceListingCounts(member_count=4, follower_count=1)
