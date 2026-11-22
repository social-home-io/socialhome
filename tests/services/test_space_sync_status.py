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
    RESULT_REFRESH_S,
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
        #: ``(space, household) ->`` when its last result came.
        self.at: dict[tuple[str, str], str] = {}

    async def record_outcome(self, space_id, instance_id, result, *, at, ok_at=None):
        self.writes.append((space_id, instance_id, result, ok_at))
        key = (space_id, instance_id)
        if key not in self.rows:
            return
        _, old_ok = self.rows[key]
        self.rows[key] = (result, ok_at if ok_at is not None else old_ok)
        self.at[key] = at

    async def outcomes(self, space_id, instance_ids):
        return [
            SpaceSyncOutcome(
                instance_id=i, result=r, ok_at=ok, result_at=self.at.get((space_id, i))
            )
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
    #: ``(space, provider)`` of the streams arriving now.
    running: set[tuple[str, str]] = set()

    async def acting_hosts(space: Space) -> list[str]:
        return list(hosts.get(space.id, [space.owner_instance_id]))

    def receiving(space_id: str, providers, ignore_sync_id: str = "") -> bool:
        return any((space_id, p) in running for p in providers)

    svc = SpaceSyncStatusService(
        bus=bus,
        space_repo=spaces,
        outcomes=outcomes,
        outbox=outbox,
        hosts=acting_hosts,
        own_instance_id="self",
        receiving=receiving,
        clock=clock,
        recheck_spacing_s=0.0,
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
    await env.svc.settle()
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.HOST_OFFLINE
    assert status.last_ok_at == NOW.isoformat()


async def test_an_answer_from_a_household_not_acting_as_host_is_not_recorded(env):
    await _pub(
        env, SpaceSyncComplete(space_id="sp", from_instance="member-x", clean=True)
    )
    await env.svc.host_unreachable("sp", ["member-x"])
    await env.svc.settle()
    assert env.outcomes.writes == []


async def test_a_repeated_refusal_is_written_once(env):
    for _ in range(3):
        await _pub(env, SpaceSyncDeferred(space_id="sp", provider_instance_id="owner"))
    assert len(env.outcomes.writes) == 1


async def test_a_repeated_result_refreshes_its_time_once_per_round(env):
    """A host that keeps refusing still answers: its result's time moves
    (at most once a round), so it never reads as gone quiet."""
    await _pub(env, SpaceSyncDeferred(space_id="sp", provider_instance_id="owner"))
    assert env.outcomes.at[("sp", "owner")] == NOW.isoformat()
    later = NOW + timedelta(seconds=RESULT_REFRESH_S + 1)
    env.clock.now = later
    await _pub(env, SpaceSyncDeferred(space_id="sp", provider_instance_id="owner"))
    assert len(env.outcomes.writes) == 2
    assert env.outcomes.at[("sp", "owner")] == later.isoformat()
    assert env.outcomes.rows[("sp", "owner")] == (SpaceSyncResult.DEFERRED, None)


async def test_ok_answers_refresh_the_time_at_most_once_per_round(env):
    for _ in range(3):
        await _pub(
            env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True)
        )
    assert len(env.outcomes.writes) == 1
    env.clock.now = NOW + timedelta(seconds=RESULT_REFRESH_S + 1)
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
    env.running.add(("sp", "owner"))
    assert await _state(env) is SpaceSyncState.CATCHING_UP


async def test_a_stream_from_a_household_not_acting_as_host_is_not_catching_up(env):
    """A co-member's stream says nothing about the host's view of the
    space: only a stream from an acting host is "catching up"."""
    env.running.add(("sp", "member-x"))
    assert await _state(env) is SpaceSyncState.NEVER


async def test_no_ok_for_over_two_hours_reads_behind(env):
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    env.clock.now = NOW + timedelta(hours=2, minutes=5)
    assert await _state(env) is SpaceSyncState.BEHIND


async def test_a_silent_host_reads_host_offline_despite_a_stale_answer(env):
    """The delegated admin answered ``ok`` once; 90 minutes on (two rounds
    without an answer) the owner is found unreachable: nobody answers —
    host offline, not up to date."""
    env.hosts["sp"] = ["owner", "admin"]
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="admin", clean=True))
    env.clock.now = NOW + timedelta(minutes=90)
    await env.svc.host_unreachable("sp", ["owner"])
    await env.svc.settle()
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.HOST_OFFLINE
    assert status.last_ok_at == NOW.isoformat()


async def test_a_delegated_admin_acting_as_host_keeps_the_space_up_to_date(env):
    env.hosts["sp"] = ["owner", "admin"]
    await env.svc.host_unreachable("sp", ["owner"])
    await env.svc.settle()
    assert await _state(env) is SpaceSyncState.HOST_OFFLINE
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="admin", clean=True))
    assert await _state(env) is SpaceSyncState.UP_TO_DATE


async def test_a_mesh_only_host_counts_like_a_paired_one(env):
    """Where the answer came from (paired, mesh, GFS) does not matter: the
    receiver publishes the same event for each."""
    await env.svc.host_unreachable("sp", ["owner"])
    await env.svc.settle()
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert await _state(env) is SpaceSyncState.UP_TO_DATE


async def test_waiting_to_send_counts_the_spaces_outbox(env):
    env.outbox.pending["sp"] = 4
    assert (await env.svc.status("sp")).waiting_to_send == 4


# ── The frame: only when the state changes ────────────────────────────────


async def test_an_outbox_change_frames_the_new_waiting_count_once(env):
    """The "N waiting to send" chip follows the outbox: a change of the
    space's pending count sends a frame, the same count none."""
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert [f.sync["waiting_to_send"] for f in env.frames] == [0]
    env.outbox.pending["sp"] = 2
    env.svc.outbox_changed("sp")
    await env.svc.settle()
    assert [f.sync["waiting_to_send"] for f in env.frames] == [0, 2]
    assert env.frames[-1].sync["state"] == "up_to_date"
    env.svc.outbox_changed("sp")  # a row of the same payload went: still 2
    await env.svc.settle()
    assert len(env.frames) == 2
    env.outbox.pending["sp"] = 0
    env.svc.outbox_changed("sp")
    await env.svc.settle()
    assert [f.sync["waiting_to_send"] for f in env.frames] == [0, 2, 0]


async def test_an_outbox_change_on_a_space_hosted_here_frames_the_count(env):
    env.outbox.pending["mine"] = 1
    env.svc.outbox_changed("mine")
    await env.svc.settle()
    assert [(f.space_id, f.sync["waiting_to_send"]) for f in env.frames] == [
        ("mine", 1)
    ]


async def test_a_burst_of_outbox_changes_reads_the_count_a_bounded_number_of_times(
    env,
):
    """A broadcast settling hundreds of rows must not read the count once
    per row: changes while a recheck runs fold into one more."""
    reads = 0
    original = env.outbox.count_pending_for_space

    async def _counting(space_id: str) -> int:
        nonlocal reads
        reads += 1
        await asyncio.sleep(0)
        return await original(space_id)

    env.outbox.count_pending_for_space = _counting
    for _ in range(200):
        env.svc.outbox_changed("sp")
    await env.svc.settle()
    assert reads <= 2


async def test_an_outbox_change_for_an_unknown_space_is_ignored(env):
    env.svc.outbox_changed("nope")
    env.svc.outbox_changed("")
    await env.svc.settle()
    assert env.frames == []


async def test_a_frame_goes_out_only_when_the_state_changes(env):
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert [f.sync["state"] for f in env.frames] == ["up_to_date"]
    env.clock.now = NOW + timedelta(seconds=RESULT_REFRESH_S + 1)
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert len(env.frames) == 1  # still up to date: no frame
    await env.svc.host_unreachable("sp", ["owner"])
    await env.svc.settle()
    env.clock.now = NOW + timedelta(hours=3)
    await env.svc.host_unreachable("sp", ["owner"])
    await env.svc.settle()
    assert [f.sync["state"] for f in env.frames] == ["up_to_date", "host_offline"]
    assert env.frames[-1].space_id == "sp"
    assert (
        env.frames[-1].sync["last_ok_at"]
        == (NOW + timedelta(seconds=RESULT_REFRESH_S + 1)).isoformat()
    )


async def test_a_stream_starting_on_a_behind_space_sends_catching_up(env):
    env.running.add(("sp", "owner"))
    env.svc.stream_started("sp", "owner")
    await env.svc.settle()
    assert [f.sync["state"] for f in env.frames] == ["catching_up"]
    env.svc.stream_started("sp", "owner")
    await env.svc.settle()
    assert len(env.frames) == 1


async def test_a_stream_from_a_non_host_sends_nothing(env):
    env.running.add(("sp", "member-x"))
    env.svc.stream_started("sp", "member-x")
    await env.svc.settle()
    assert env.frames == []


async def test_no_frame_for_a_space_hosted_here(env):
    env.svc.stream_started("mine", "self")
    await env.svc.settle()
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


# ── One recheck path per space: no storm, no stale frame ──────────────────


async def test_concurrent_triggers_never_end_on_a_stale_frame(env):
    """An outcome and an outbox change race: the slower recheck must never
    send its older view after the newer one. The last frame is the state
    and count the space has now."""
    gate = asyncio.Event()
    original = env.outbox.count_pending_for_space
    calls = 0

    async def _slow_first(space_id: str) -> int:
        nonlocal calls
        calls += 1
        value = await original(space_id)
        if calls == 1:
            await gate.wait()  # the first recheck read its count, then stalls
        return value

    env.outbox.count_pending_for_space = _slow_first
    env.svc.outbox_changed("sp")  # reads waiting=0, then stalls
    await asyncio.sleep(0.01)
    env.outbox.pending["sp"] = 3
    await env.bus.publish(
        SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True)
    )
    await asyncio.sleep(0.01)
    gate.set()
    await env.svc.settle()
    assert env.frames[-1].sync["state"] == "up_to_date"
    assert env.frames[-1].sync["waiting_to_send"] == 3


def _spaced(env, spacing: float) -> SpaceSyncStatusService:
    return SpaceSyncStatusService(
        bus=env.bus,
        space_repo=_Spaces(_space("sp")),
        outcomes=env.outcomes,
        outbox=env.outbox,
        hosts=_owner_only,
        own_instance_id="self",
        clock=env.clock,
        recheck_spacing_s=spacing,
    )


async def test_a_burst_of_a_thousand_changes_sends_a_few_frames(env):
    svc = _spaced(env, 0.05)
    for i in range(1000):
        env.outbox.pending["sp"] = i
        svc.outbox_changed("sp")
        if i % 50 == 0:
            await asyncio.sleep(0)
    await svc.settle()
    assert 1 <= len(env.frames) <= 3
    assert env.frames[-1].sync["waiting_to_send"] == 999


async def test_reruns_are_spaced_a_fixed_while_apart(env):
    """A re-run that came due waits the fixed spacing: at most one frame
    per spacing per space."""
    spaced = _spaced(env, 0.2)
    loop = asyncio.get_running_loop()
    times: list[float] = []

    async def _stamp(event: SpaceSyncStateChanged) -> None:
        times.append(loop.time())

    env.bus.subscribe(SpaceSyncStateChanged, _stamp)
    for i in range(3):
        env.outbox.pending["sp"] = i + 1
        spaced.outbox_changed("sp")
        await asyncio.sleep(0.05)
    await spaced.settle()
    assert len(times) == 2  # the first at once, the rest folded into one
    assert times[1] - times[0] >= 0.2


async def test_stream_started_never_blocks_the_chunk_path(env):
    """The receiver calls it on a stream's first chunk: it returns at once,
    the reads happen in the status's own task."""
    env.running.add(("sp", "owner"))
    assert env.svc.stream_started("sp", "owner") is None
    assert env.frames == []
    await env.svc.settle()
    assert [f.sync["state"] for f in env.frames] == ["catching_up"]


async def test_stop_records_what_is_due_then_takes_no_more(env):
    """An outcome published before shutdown is still written; after
    ``stop()`` no trigger writes or frames anything."""
    await env.bus.publish(
        SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True)
    )
    await env.svc.stop()
    assert env.outcomes.rows[("sp", "owner")][0] is SpaceSyncResult.OK
    writes, frames = len(env.outcomes.writes), len(env.frames)
    env.outbox.pending["sp"] = 7
    env.running.add(("sp", "owner"))
    env.clock.now = NOW + timedelta(hours=1)
    env.svc.outbox_changed("sp")
    env.svc.stream_started("sp", "owner")
    await env.bus.publish(
        SpaceSyncDeferred(space_id="sp", provider_instance_id="owner")
    )
    await asyncio.sleep(0.05)
    await env.svc.settle()
    assert len(env.outcomes.writes) == writes
    assert len(env.frames) == frames


async def test_stop_cancels_what_does_not_finish_in_time(env):
    """A recheck stuck on a read is cancelled once the timeout passes:
    ``stop()`` returns, and nothing is framed from it."""
    entered = asyncio.Event()
    cancelled: list[bool] = []

    async def _hang(space_id: str) -> int:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return 0

    env.outbox.count_pending_for_space = _hang
    env.svc.outbox_changed("sp")
    await entered.wait()
    await asyncio.wait_for(env.svc.stop(timeout=0.05), timeout=1.0)
    assert cancelled == [True]
    assert env.frames == []


# ── v_95 sync pages ─────────────────────────────────────────────────────


def _position(**over) -> SyncPagePosition:
    fields = {
        "seq": 812,
        "at": NOW.isoformat(),
        "signer_role": "owner",
        "match": True,
        "missing": 0,
        "differing_buckets": 0,
    }
    fields.update(over)
    return SyncPagePosition(**fields)


async def test_up_to_date_as_of_the_page_while_the_host_is_offline(env):
    """The host went silent, but our copy matches its newest page
    (relayed by any member): up to date, checked against page N."""
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


async def test_behind_when_a_listing_proved_a_row_missing(env):
    async def position(space, hosts):
        return _position(match=False, missing=2, differing_buckets=3)

    env.svc.attach_pages(position)
    await env.svc.host_unreachable("sp", ["owner"])
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.BEHIND
    assert status.to_wire()["page"]["missing"] == 2


async def test_no_verdict_yet_keeps_the_phase_1_state(env):
    async def position(space, hosts):
        return _position(match=None, missing=None, differing_buckets=3)

    env.svc.attach_pages(position)
    await env.svc.host_unreachable("sp", ["owner"])
    status = await env.svc.status("sp")
    assert status.state is SpaceSyncState.HOST_OFFLINE


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
    await env.svc.settle()
    env.frames.clear()
    current[0] = _position()
    # Through the one coalesced recheck path, like every other trigger.
    await env.svc.page_changed("sp")
    await env.svc.settle()
    assert [f.sync["state"] for f in env.frames] == ["up_to_date"]
    await env.svc.page_changed("sp")
    await env.svc.settle()
    assert len(env.frames) == 1
    await env.svc.page_changed("unknown")
    await env.svc.settle()
    assert len(env.frames) == 1


# ── "Some posts may be missing" ────────────────────────────────────────────


async def test_a_proven_missing_row_may_be_missing(env):
    async def position(space, hosts):
        return _position(match=False, missing=2, differing_buckets=3)

    env.svc.attach_pages(position)
    status = await env.svc.status("sp")
    assert status.may_be_missing is True
    assert await env.svc.may_be_missing("sp") is True


async def test_a_first_mismatch_without_a_proof_is_no_warning(env):
    async def position(space, hosts):
        return _position(match=None, missing=None, differing_buckets=3)

    env.svc.attach_pages(position)
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    assert (await env.svc.status("sp")).may_be_missing is False


async def test_two_failed_rounds_after_an_ok_may_be_missing(env):
    await _pub(env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=True))
    env.clock.now = NOW + timedelta(minutes=30)
    await _pub(
        env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=False)
    )
    assert await env.svc.may_be_missing("sp") is False
    env.clock.now = NOW + timedelta(minutes=60)
    await _pub(
        env, SpaceSyncComplete(space_id="sp", from_instance="owner", clean=False)
    )
    assert await env.svc.may_be_missing("sp") is True
    # The warning goes out as a frame.
    assert env.frames[-1].sync["may_be_missing"] is True


async def test_a_space_hosted_here_or_unknown_is_never_missing(env):
    assert await env.svc.may_be_missing("mine") is False
    assert await env.svc.may_be_missing("unknown") is False


# ── "Not sent yet" ─────────────────────────────────────────────────────────


async def test_unsent_posts_come_from_the_delivery_check(env):
    asked: list[str] = []

    async def undelivered(space_id: str) -> list[str]:
        asked.append(space_id)
        return ["p2", "p1"]

    env.svc.attach_delivery(undelivered)
    status = await env.svc.status("sp")
    assert status.unsent_posts == ("p2", "p1")
    assert status.to_wire()["unsent_posts"] == ["p2", "p1"]
    assert asked == ["sp"]
    # The space's own host household too.
    assert (await env.svc.status("mine")).unsent_posts == ("p2", "p1")


async def test_a_post_a_gfs_took_is_sent_and_framed(env):
    async def undelivered(space_id: str) -> list[str]:
        return ["p2", "p1"]

    env.svc.attach_delivery(undelivered)
    env.outbox.pending["sp"] = 2
    env.svc.outbox_changed("sp")
    await env.svc.settle()
    assert env.frames[-1].sync["unsent_posts"] == ["p2", "p1"]
    env.svc.post_reached_gfs("sp", "p2")
    await env.svc.settle()
    assert env.frames[-1].sync["unsent_posts"] == ["p1"]
    assert (await env.svc.status("sp")).unsent_posts == ("p1",)


async def test_unsent_posts_are_bounded(env):
    async def undelivered(space_id: str) -> list[str]:
        return [f"p{i}" for i in range(80)]

    env.svc.attach_delivery(undelivered)
    env.outbox.pending["sp"] = 80
    assert len((await env.svc.status("sp")).unsent_posts) == 50


async def test_without_the_delivery_check_nothing_is_unsent(env):
    env.outbox.pending["sp"] = 3
    assert (await env.svc.status("sp")).unsent_posts == ()
