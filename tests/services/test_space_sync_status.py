"""Tests for :class:`SpaceSyncStatusService` — persisting each sync outcome
of a space's acting hosts and telling a member whether it needs sync."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.events import (
    SpaceSyncComplete,
    SpaceSyncDeferred,
    SpaceSyncStateChanged,
)
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.space_sync_page import SyncPagePosition
from socialhome.domain.space_sync_status import (
    SpaceSyncOutcome,
    SpaceSyncResult,
    SpaceSyncState,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.space_sync_status import (
    OK_REFRESH_S,
    SpaceSyncStatusService,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _space(space_id: str = "sp", owner: str = "owner") -> Space:
    return Space(
        id=space_id,
        name=space_id,
        owner_instance_id=owner,
        owner_username="anna",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )


class _Spaces:
    def __init__(self, *spaces: Space) -> None:
        self.spaces = {s.id: s for s in spaces}

    async def get(self, space_id: str) -> Space | None:
        await asyncio.sleep(0)  # a real read suspends (the executor)
        return self.spaces.get(space_id)


class _Outcomes:
    """In-memory ``record_outcome`` / ``outcomes`` over seat rows."""

    def __init__(self, seats: set[tuple[str, str]]) -> None:
        self.rows: dict[tuple[str, str], tuple[SpaceSyncResult | None, str | None]] = {
            seat: (None, None) for seat in seats
        }
        self.writes: list[tuple[str, str, SpaceSyncResult, str | None]] = []

    async def record_outcome(self, space_id, instance_id, result, *, ok_at=None):
        self.writes.append((space_id, instance_id, result, ok_at))
        key = (space_id, instance_id)
        if key not in self.rows:
            return
        _, old_ok = self.rows[key]
        self.rows[key] = (result, ok_at if ok_at is not None else old_ok)

    async def outcomes(self, space_id, instance_ids):
        return [
            SpaceSyncOutcome(instance_id=i, result=r, ok_at=ok)
            for i in instance_ids
            if (space_id, i) in self.rows
            for r, ok in [self.rows[(space_id, i)]]
        ]


class _Outbox:
    def __init__(self) -> None:
        self.pending: dict[str, int] = {}

    async def count_pending_for_space(self, space_id: str) -> int:
        return self.pending.get(space_id, 0)


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def env():
    bus = EventBus()
    spaces = _Spaces(_space("sp"), _space("mine", owner="self"))
    outcomes = _Outcomes({("sp", "owner"), ("sp", "admin"), ("sp", "member-x")})
    outbox = _Outbox()
    clock = _Clock()
    hosts = {"sp": ["owner"]}
    running: set[str] = set()

    async def acting_hosts(space: Space) -> list[str]:
        return list(hosts.get(space.id, [space.owner_instance_id]))

    def receiving(space_id: str, ignore_sync_id: str = "") -> bool:
        return space_id in running

    svc = SpaceSyncStatusService(
        bus=bus,
        space_repo=spaces,
        outcomes=outcomes,
        outbox=outbox,
        hosts=acting_hosts,
        own_instance_id="self",
        receiving=receiving,
        clock=clock,
    )
    svc.wire()
    frames: list[SpaceSyncStateChanged] = []

    async def _frame(event: SpaceSyncStateChanged) -> None:
        frames.append(event)

    bus.subscribe(SpaceSyncStateChanged, _frame)

    class Env:
        pass

    e = Env()
    e.__dict__.update(
        bus=bus,
        svc=svc,
        outcomes=outcomes,
        outbox=outbox,
        clock=clock,
        hosts=hosts,
        running=running,
        frames=frames,
    )
    return e


async def _pub(env, event) -> None:
    """Publish and wait for the outcome to be recorded (its own task)."""
    await env.bus.publish(event)
    await env.svc.settle()


async def _state(env, space_id: str = "sp") -> SpaceSyncState:
    status = await env.svc.status(space_id)
    assert status is not None
    return status.state


# ── Outcome persistence ────────────────────────────────────────────────────


async def test_nothing_recorded_reads_never(env):
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.NEVER
    assert status.last_ok_at is None


async def test_a_clean_stream_from_the_host_is_ok(env):
    await _pub(
        env,
        SpaceSyncComplete(
            space_id="sp", from_instance="owner", sync_id="s1", clean=True
        ),
    )
    assert env.outcomes.writes == [("sp", "owner", SpaceSyncResult.OK, NOW.isoformat())]
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.UP_TO_DATE
    assert status.last_ok_at == NOW.isoformat()


async def test_the_quiet_sentinel_is_ok(env):
    """An up-to-date space's whole answer is the sentinel of an empty
    session — the receiver publishes it as a clean ``SpaceSyncComplete``."""
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert await _state(env) is SpaceSyncState.UP_TO_DATE


async def test_an_unclean_stream_is_behind(env):
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    await _pub(
        env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=False)
    )
    assert env.outcomes.rows[("sp", "owner")] == (
        SpaceSyncResult.UNCLEAN,
        NOW.isoformat(),
    )
    assert await _state(env) is SpaceSyncState.BEHIND


async def test_a_deferral_is_recorded(env):
    await _pub(
        env,
        SpaceSyncDeferred(
            space_id="sp", provider_instance_id="owner", reason="too_many_sessions"
        ),
    )
    assert env.outcomes.rows[("sp", "owner")] == (SpaceSyncResult.DEFERRED, None)


async def test_a_silent_host_is_unreachable_and_reads_host_offline(env):
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    env.clock.now = NOW + timedelta(hours=3)
    await env.svc.host_unreachable("sp", ["owner"])
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.HOST_OFFLINE
    assert status.last_ok_at == NOW.isoformat()


async def test_an_answer_from_a_household_not_acting_as_host_is_not_recorded(env):
    await _pub(
        env, SpaceSyncComplete(space_id="sp", from_instance="member-x", clean=True)
    )
    await env.svc.host_unreachable("sp", ["member-x"])
    assert env.outcomes.writes == []


async def test_a_repeated_refusal_is_written_once(env):
    for _ in range(3):
        await _pub(env, SpaceSyncDeferred(space_id="sp", provider_instance_id="owner"))
    assert len(env.outcomes.writes) == 1


async def test_ok_answers_refresh_the_time_at_most_once_per_round(env):
    for _ in range(3):
        await _pub(
            env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True)
        )
    assert len(env.outcomes.writes) == 1
    env.clock.now = NOW + timedelta(seconds=OK_REFRESH_S + 1)
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert len(env.outcomes.writes) == 2


async def test_a_new_process_reads_the_recorded_outcome(env):
    """The outcome lives in the database: a fresh service (a restart)
    reads it back instead of "never"."""
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    fresh = SpaceSyncStatusService(
        bus=EventBus(),
        space_repo=_Spaces(_space("sp")),
        outcomes=env.outcomes,
        outbox=env.outbox,
        hosts=lambda space: _owner_only(space),
        own_instance_id="self",
        clock=env.clock,
    )
    assert (await fresh.status("sp")).state is SpaceSyncState.UP_TO_DATE


async def _owner_only(space: Space) -> list[str]:
    return [space.owner_instance_id]


# ── The read-time states ──────────────────────────────────────────────────


async def test_the_owner_household_always_reads_up_to_date(env):
    env.outbox.pending["mine"] = 2
    status = await env.svc.status("mine")
    assert status.state is SpaceSyncState.UP_TO_DATE
    assert status.waiting_to_send == 2


async def test_an_unknown_space_has_no_status(env):
    assert await env.svc.status("nope") is None


async def test_a_running_stream_reads_catching_up(env):
    env.running.add("sp")
    assert await _state(env) is SpaceSyncState.CATCHING_UP


async def test_no_ok_for_over_two_hours_reads_behind(env):
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    env.clock.now = NOW + timedelta(hours=2, minutes=5)
    assert await _state(env) is SpaceSyncState.BEHIND


async def test_a_delegated_admin_acting_as_host_keeps_the_space_up_to_date(env):
    env.hosts["sp"] = ["owner", "admin"]
    await env.svc.host_unreachable("sp", ["owner"])
    assert await _state(env) is SpaceSyncState.HOST_OFFLINE
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="admin", clean=True))
    assert await _state(env) is SpaceSyncState.UP_TO_DATE


async def test_a_mesh_only_host_counts_like_a_paired_one(env):
    """Where the answer came from (paired, mesh, GFS) does not matter: the
    receiver publishes the same event for each."""
    await env.svc.host_unreachable("sp", ["owner"])
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert await _state(env) is SpaceSyncState.UP_TO_DATE


async def test_waiting_to_send_counts_the_spaces_outbox(env):
    env.outbox.pending["sp"] = 4
    assert (await env.svc.status("sp")).waiting_to_send == 4


# ── The frame: only when the state changes ────────────────────────────────


async def test_a_frame_goes_out_only_when_the_state_changes(env):
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert [f.sync["state"] for f in env.frames] == ["up_to_date"]
    env.clock.now = NOW + timedelta(seconds=OK_REFRESH_S + 1)
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert len(env.frames) == 1  # still up to date: no frame
    await env.svc.host_unreachable("sp", ["owner"])
    env.clock.now = NOW + timedelta(hours=3)
    await env.svc.host_unreachable("sp", ["owner"])
    assert [f.sync["state"] for f in env.frames] == ["up_to_date", "host_offline"]
    assert env.frames[-1].space_id == "sp"
    assert (
        env.frames[-1].sync["last_ok_at"]
        == (NOW + timedelta(seconds=OK_REFRESH_S + 1)).isoformat()
    )


async def test_a_stream_starting_on_a_behind_space_sends_catching_up(env):
    env.running.add("sp")
    await env.svc.stream_started("sp", "owner")
    assert [f.sync["state"] for f in env.frames] == ["catching_up"]
    await env.svc.stream_started("sp", "owner")
    assert len(env.frames) == 1


async def test_a_stream_from_a_non_host_sends_nothing(env):
    env.running.add("sp")
    await env.svc.stream_started("sp", "member-x")
    assert env.frames == []


async def test_no_frame_for_a_space_hosted_here(env):
    await env.svc.stream_started("mine", "self")
    await _pub(
        env, SpaceSyncComplete(space_id="mine", from_instance="peer", clean=True)
    )
    assert env.frames == []


async def test_an_answer_before_the_hosts_seat_row_is_not_remembered_as_written(env):
    """Regression (demo): the host's seat row can arrive after its first
    answer. That answer has nowhere to go — but the next one, right after,
    must still be written, not skipped as "refreshed this round"."""
    env.hosts["sp"] = ["owner", "late"]
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="late", clean=True))
    assert ("sp", "late") not in env.outcomes.rows
    env.outcomes.rows[("sp", "late")] = (None, None)
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="late", clean=True))
    assert env.outcomes.rows[("sp", "late")] == (SpaceSyncResult.OK, NOW.isoformat())
    assert await _state(env) is SpaceSyncState.UP_TO_DATE


async def test_the_outcome_survives_the_publisher_being_cancelled(env):
    """Regression (demo): the receiver publishes a stream's end from the
    session's drain task, and the federation service's handler of the same
    event — subscribed earlier — closes the session, which cancels that very
    task while the bus goes on to the next handlers. The outcome must still
    be recorded."""
    bus = EventBus()

    async def _closes_the_session(_event) -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()

    bus.subscribe(SpaceSyncComplete, _closes_the_session)
    svc = SpaceSyncStatusService(
        bus=bus,
        space_repo=_Spaces(_space("sp")),
        outcomes=env.outcomes,
        outbox=env.outbox,
        hosts=_owner_only,
        own_instance_id="self",
        clock=env.clock,
    )
    svc.wire()
    publisher = asyncio.create_task(
        bus.publish(SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    )
    await asyncio.gather(publisher, return_exceptions=True)
    assert publisher.cancelled()
    await svc.settle()
    assert env.outcomes.rows[("sp", "owner")] == (SpaceSyncResult.OK, NOW.isoformat())

# ── v_95 sync pages ─────────────────────────────────────────────────────


def _position(**over) -> SyncPagePosition:
    fields = {
        "seq": 812,
        "at": NOW.isoformat(),
        "signer_role": "owner",
        "applied": True,
        "match": None,
        "differing_buckets": None,
    }
    fields.update(over)
    return SyncPagePosition(**fields)


async def test_up_to_date_as_of_the_page_while_the_host_is_offline(env):
    """The host went silent, but we applied its stream at or past its
    newest page (relayed by any member): up to date as of page N."""
    seen: list[tuple[str, list[str]]] = []

    async def position(space, hosts):
        seen.append((space.id, hosts))
        return _position()

    env.svc.attach_pages(position)
    await env.svc.host_unreachable("sp", ["owner"])
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.UP_TO_DATE
    assert status.to_wire()["page"]["seq"] == 812
    assert seen[-1] == ("sp", ["owner"])


async def test_behind_when_a_newer_signed_page_exists(env):
    async def position(space, hosts):
        return _position(applied=False)

    env.svc.attach_pages(position)
    await env.svc.host_unreachable("sp", ["owner"])
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.BEHIND
    assert status.to_wire()["page"]["applied"] is False


async def test_no_page_keeps_the_phase_1_status_and_wire(env):
    async def position(space, hosts):
        return None

    env.svc.attach_pages(position)
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.NEVER
    assert "page" not in status.to_wire()


async def test_a_new_page_sends_a_frame_when_the_state_moves(env):
    current: list[SyncPagePosition | None] = [None]

    async def position(space, hosts):
        return current[0]

    env.svc.attach_pages(position)
    await env.svc.host_unreachable("sp", ["owner"])
    env.frames.clear()
    current[0] = _position()
    await env.svc.recheck("sp")
    assert [f.sync["state"] for f in env.frames] == ["up_to_date"]
    await env.svc.recheck("sp")
    assert len(env.frames) == 1
    await env.svc.recheck("mine")  # hosted here: no frame
    await env.svc.recheck("unknown")
    assert len(env.frames) == 1
