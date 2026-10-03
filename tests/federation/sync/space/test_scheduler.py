"""Unit tests for :class:`SpaceSyncScheduler`."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from socialhome.domain.events import (
    PairingConfirmed,
    SpaceAuthorityEchoDue,
    SpaceSyncComplete,
    SpaceSyncDeferred,
)
from socialhome.domain.federation import FederationEventType, PairingStatus
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.federation.sync.space import scheduler as scheduler_mod
from socialhome.federation.sync.space.scheduler import (
    MAX_MESH_CATCHUP_ATTEMPTS,
    STALE_SESSION_TTL_SECONDS,
    SpaceSyncScheduler,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.reconnect_queue import ReconnectSyncQueue


class _FakeFederation:
    def __init__(self, *, confirmed: set[str] | None = None):
        self.sent: list[dict] = []
        #: Instance ids ``is_confirmed_peer`` answers True for. Anything
        #: else is treated as reachable only over the mesh.
        self.confirmed: set[str] = confirmed if confirmed is not None else set()
        #: (space_id, host_instance_id) for each mesh catch-up BEGIN.
        self.mesh_catchups: list[tuple[str, str]] = []
        #: What ``begin_mesh_catchup_sync`` reports. False models the
        #: ``no_route`` a freshly-booted household gets.
        self.catchup_ships: bool = True
        #: (sync_id, space_id, provider) recorded before each BEGIN.
        self.requests: list[tuple[str, str, str]] = []
        #: The ``extra_payload`` (v_46 authority echo) of each catch-up.
        self.mesh_extras: list[dict | None] = []

    def record_sync_request(self, *, sync_id, space_id, provider_instance_id):
        """A requester notes the sync_id it is about to ask for, so the
        provider's SPACE_SYNC_OFFER can be recognised as an answer."""
        self.requests.append((sync_id, space_id, provider_instance_id))

    async def is_confirmed_peer(self, instance_id: str) -> bool:
        return instance_id in self.confirmed

    async def begin_mesh_catchup_sync(
        self, *, space_id, host_instance_id, extra_payload=None
    ):
        self.mesh_catchups.append((space_id, host_instance_id))
        self.mesh_extras.append(extra_payload)
        # Mirrors the real return contract: True = the BEGIN shipped, False =
        # a transient failure (``no_route`` on a still-warming mesh).
        return self.catchup_ships

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
                "space_id": space_id,
            }
        )
        return SimpleNamespace(ok=True)


class _FakeFedRepo:
    def __init__(self, instances):
        self._instances = instances

    async def list_social_instances(self):
        return await self.list_instances(status="confirmed")

    async def list_instances(self, *, source=None, status=None):
        return self._instances


class _FakeSpaceRepo:
    def __init__(self, *, spaces_by_type, members_by_space):
        self._spaces = spaces_by_type
        self._members = members_by_space

    async def list_by_type(self, space_type):
        return self._spaces.get(space_type, [])

    async def list_member_instances(self, space_id):
        return self._members.get(space_id, [])


def _space(space_id: str, *, owner_instance_id: str = "self") -> Space:
    return Space(
        id=space_id,
        name=space_id,
        owner_instance_id=owner_instance_id,
        owner_username="admin",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.HOUSEHOLD,
        join_mode=JoinMode.INVITE_ONLY,
    )


def _peer(instance_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=instance_id,
        status=PairingStatus.CONFIRMED,
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def queue():
    return ReconnectSyncQueue(concurrency=2)


@pytest.fixture
def sync_manager():
    mgr = MagicMock()
    mgr.reap_stale = MagicMock(return_value=0)
    return mgr


async def test_on_pairing_confirmed_enqueues_for_shared_spaces(
    bus, queue, sync_manager
):
    fed = _FakeFederation()
    spaces = {SpaceType.HOUSEHOLD: [_space("sp-1"), _space("sp-2")]}
    # The new peer is only a member of sp-1.
    members = {"sp-1": ["peer-a"], "sp-2": ["other-peer"]}
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([_peer("peer-a")]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type=spaces,
            members_by_space=members,
        ),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    sched.wire()
    await queue.start()
    try:
        await bus.publish(PairingConfirmed(instance_id="peer-a"))
        # Let the worker drain.
        await asyncio.sleep(0.1)
    finally:
        await queue.stop()
    # Expect one SPACE_SYNC_BEGIN sent for sp-1 to peer-a (not sp-2).
    beg = [s for s in fed.sent if s["type"] == FederationEventType.SPACE_SYNC_BEGIN]
    assert len(beg) == 1
    assert beg[0]["to"] == "peer-a"
    assert beg[0]["space_id"] == "sp-1"


async def test_on_pairing_confirmed_ignores_self(bus, queue, sync_manager):
    fed = _FakeFederation()
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type={SpaceType.HOUSEHOLD: [_space("sp-1")]},
            members_by_space={"sp-1": ["self"]},
        ),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    sched.wire()
    await queue.start()
    try:
        await bus.publish(PairingConfirmed(instance_id="self"))
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    assert fed.sent == []


async def test_enqueue_sync_for_space_sends_begin(bus, queue, sync_manager):
    fed = _FakeFederation()
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type={},
            members_by_space={},
        ),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    await queue.start()
    try:
        await sched.enqueue_sync_for_space(
            space_id="sp-1",
            peer_instance_id="peer-a",
        )
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    assert len(fed.sent) == 1
    assert fed.sent[0]["type"] == FederationEventType.SPACE_SYNC_BEGIN
    assert fed.sent[0]["payload"]["space_id"] == "sp-1"
    assert fed.sent[0]["payload"]["sync_mode"] == "initial"
    assert fed.sent[0]["payload"]["prefer_direct"] is True


async def test_periodic_tick_enqueues_for_every_confirmed_peer(
    bus, queue, sync_manager
):
    fed = _FakeFederation()
    # Two confirmed peers, one shared space.
    spaces = {SpaceType.HOUSEHOLD: [_space("sp-1")]}
    members = {"sp-1": ["peer-a", "peer-b"]}
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([_peer("peer-a"), _peer("peer-b")]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type=spaces,
            members_by_space=members,
        ),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    await queue.start()
    try:
        await sched._tick_once()
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    # One SPACE_SYNC_BEGIN per peer.
    beg = [s for s in fed.sent if s["type"] == FederationEventType.SPACE_SYNC_BEGIN]
    assert {s["to"] for s in beg} == {"peer-a", "peer-b"}


async def test_start_stop_idempotent(bus, queue, sync_manager):
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=_FakeFederation(),
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(spaces_by_type={}, members_by_space={}),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    await sched.start()
    await sched.start()  # second call is a no-op
    await sched.stop()
    await sched.stop()  # second call is a no-op


async def test_tick_reaps_stale_sync_sessions(bus, queue, sync_manager):
    """The periodic tick drives the sync-session TTL reaper backstop."""
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=_FakeFederation(),
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(spaces_by_type={}, members_by_space={}),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    await sched._tick_once()
    sync_manager.reap_stale.assert_called_once_with(STALE_SESSION_TTL_SECONDS)


async def test_tick_survives_reaper_exception(bus, queue, sync_manager):
    """A reaper error never kills the tick — the sync enqueue work still runs."""
    sync_manager.reap_stale.side_effect = RuntimeError("boom")
    fed = _FakeFederation()
    spaces = {SpaceType.HOUSEHOLD: [_space("sp-1")]}
    members = {"sp-1": ["peer-a"]}
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([_peer("peer-a")]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type=spaces,
            members_by_space=members,
        ),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    await queue.start()
    try:
        await sched._tick_once()
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    beg = [s for s in fed.sent if s["type"] == FederationEventType.SPACE_SYNC_BEGIN]
    assert {s["to"] for s in beg} == {"peer-a"}


# ── #648: mesh-only hosts get a catch-up BEGIN ───────────────────────


def _mesh_sched(bus, queue, sync_manager, fed, spaces):
    return SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type={SpaceType.PRIVATE: spaces},
            members_by_space={},
        ),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )


async def test_tick_begins_catchup_for_mesh_only_host(bus, queue, sync_manager):
    """A space hosted by a non-confirmed peer gets a catch-up BEGIN.

    Triggers 1 and 2 both walk CONFIRMED peers, so neither ever fires for
    a mesh-only host — which is why a restarted requester stayed empty
    forever (#648). ``Space.owner_instance_id`` is the host, so the
    predicate is exact rather than a heuristic.
    """
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-mesh", owner_instance_id="host-x")],
    )

    await sched._tick_once()

    assert fed.mesh_catchups == [("sp-mesh", "host-x")]
    # Must NOT go out via the confirmed-peer path, which ships a bare
    # send_event with no requester-side receive session.
    assert fed.sent == []


async def test_tick_skips_confirmed_host(bus, queue, sync_manager):
    """A confirmed host is already covered by the periodic sweep."""
    fed = _FakeFederation(confirmed={"host-x"})
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-direct", owner_instance_id="host-x")],
    )

    await sched._tick_once()

    assert fed.mesh_catchups == []


async def test_tick_skips_our_own_space(bus, queue, sync_manager):
    """We are the host — there is nobody to catch up from."""
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-own", owner_instance_id="self")],
    )

    await sched._tick_once()

    assert fed.mesh_catchups == []


async def test_completion_sentinel_stops_the_retries(bus, queue, sync_manager):
    """The end-of-stream sentinel is the watermark — not a content check.

    ``stream_initial`` emits ``__complete__`` unconditionally, even for a
    space with zero rows, so a legitimately empty space completes on the
    first attempt and never re-BEGINs. Using "has no posts" as the signal
    would loop on it forever.
    """
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-mesh", owner_instance_id="host-x")],
    )
    sched.wire()

    await sched._tick_once()
    assert fed.mesh_catchups == [("sp-mesh", "host-x")]

    # The receiver publishes this when the sentinel chunk lands.
    await bus.publish(
        SpaceSyncComplete(space_id="sp-mesh", from_instance="host-x"),
    )

    await sched._tick_once()
    assert fed.mesh_catchups == [("sp-mesh", "host-x")], "re-BEGAN after completing"


async def test_mesh_catchup_attempts_are_capped(bus, queue, sync_manager):
    """An unreachable host can't burn the 5/h BEGIN budget forever."""
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-mesh", owner_instance_id="host-x")],
    )

    for _ in range(MAX_MESH_CATCHUP_ATTEMPTS + 3):
        await sched._tick_once()

    assert len(fed.mesh_catchups) == MAX_MESH_CATCHUP_ATTEMPTS


async def test_mesh_catchup_failure_does_not_kill_the_tick(bus, queue, sync_manager):
    """A raising ``begin_mesh_catchup_sync`` must not skip ``reap_stale``."""
    fed = _FakeFederation(confirmed=set())

    async def _boom(*, space_id, host_instance_id):
        raise RuntimeError("mesh down")

    fed.begin_mesh_catchup_sync = _boom  # type: ignore[assignment]
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-mesh", owner_instance_id="host-x")],
    )

    await sched._tick_once()

    sync_manager.reap_stale.assert_called_once_with(STALE_SESSION_TTL_SECONDS)


async def test_transient_no_route_does_not_consume_the_attempt_cap(
    bus, queue, sync_manager
):
    """A BEGIN that never reached the host must not burn an attempt.

    A household that has just rebooted can't route anywhere yet, so the
    first sweep gets ``no_route``. Counting that against the 3-attempt cap
    (and then waiting for the 30-minute tick) is what still left a
    restarted mesh member empty even with the trigger in place — the host
    never saw the BEGIN, so its 5/h budget was never touched either.
    """
    fed = _FakeFederation(confirmed=set())
    fed.catchup_ships = False
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-mesh", owner_instance_id="host-x")],
    )

    for _ in range(5):
        pending = await sched._tick_mesh_catchup()
        assert pending == 1, "an undelivered BEGIN should stay pending"

    # Retried every pass, cap untouched.
    assert len(fed.mesh_catchups) == 5

    # Once the mesh warms up the BEGIN ships and now it counts against the
    # cap — but the pair stays pending until the sentinel lands, because a
    # delivered BEGIN only means the host STARTED streaming.
    fed.catchup_ships = True
    assert await sched._tick_mesh_catchup() == 1
    assert sched._mesh_catchup_attempts[("sp-mesh", "host-x")] == 1

    sched.wire()
    await bus.publish(SpaceSyncComplete(space_id="sp-mesh", from_instance="host-x"))
    assert await sched._tick_mesh_catchup() == 0


async def test_startup_sweep_retries_until_the_begin_ships(bus, queue, sync_manager):
    """The startup sweep keeps trying across its backoff schedule.

    Pinned because the failure it fixes is invisible in a unit test that
    only calls the tick once: the sweep fired 45 s after boot, got
    ``no_route`` while the transports were still coming up, and gave up.
    """
    fed = _FakeFederation(confirmed=set())
    fed.catchup_ships = False
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-mesh", owner_instance_id="host-x")],
    )

    # Collapse the schedule so the test doesn't actually wait minutes.
    with (
        patch.object(scheduler_mod, "STARTUP_MESH_CATCHUP_DELAY_SECONDS", 0.01),
        patch.object(
            scheduler_mod, "STARTUP_MESH_CATCHUP_RETRY_DELAYS_S", (0.01, 0.01)
        ),
    ):
        await sched._startup_mesh_catchup()

    # Initial pass + both retries.
    assert len(fed.mesh_catchups) == 3


async def test_startup_sweep_stops_once_the_sentinel_lands(bus, queue, sync_manager):
    """The sweep stops on the sentinel, not on a successful send.

    A shipped BEGIN only means the host began streaming — it can still
    abandon the stream (e.g. it cannot re-discover a route back to a
    just-rebooted requester). So "done" must be the sentinel, and a pair
    that never completes keeps being retried until the cap.
    """
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-mesh", owner_instance_id="host-x")],
    )
    sched.wire()

    # Sentinel already recorded → the sweep has nothing to do at all.
    await bus.publish(SpaceSyncComplete(space_id="sp-mesh", from_instance="host-x"))
    with (
        patch.object(scheduler_mod, "STARTUP_MESH_CATCHUP_DELAY_SECONDS", 0.01),
        patch.object(
            scheduler_mod, "STARTUP_MESH_CATCHUP_RETRY_DELAYS_S", (0.01, 0.01)
        ),
    ):
        await sched._startup_mesh_catchup()

    assert fed.mesh_catchups == []


async def test_startup_sweep_retries_a_shipped_but_incomplete_sync(
    bus, queue, sync_manager
):
    """A BEGIN that shipped but never completed is retried, up to the cap.

    This is the case that kept `sync-https-fallback` red: the host got the
    BEGIN, invalidated its cached route to the rebooted requester (as it
    must), then couldn't re-discover one because the requester→host
    direction comes up first — so it abandoned the stream and nothing
    retried until the next 30-minute tick.
    """
    fed = _FakeFederation(confirmed=set())  # ships fine, never completes
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-mesh", owner_instance_id="host-x")],
    )

    with (
        patch.object(scheduler_mod, "STARTUP_MESH_CATCHUP_DELAY_SECONDS", 0.01),
        patch.object(
            scheduler_mod,
            "STARTUP_MESH_CATCHUP_RETRY_DELAYS_S",
            (0.01, 0.01, 0.01, 0.01),
        ),
    ):
        await sched._startup_mesh_catchup()

    # Retried, and bounded by the delivered-BEGIN cap rather than running
    # for every pass in the schedule.
    assert len(fed.mesh_catchups) == MAX_MESH_CATCHUP_ATTEMPTS


async def test_tick_runs_the_roster_refresh_and_survives_its_failure(
    bus, queue, sync_manager
):
    """Every tick re-sends hosted rosters (v_32 self-heal); a failure there
    never costs the rest of the tick."""
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=_FakeFederation(),
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(spaces_by_type={}, members_by_space={}),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    calls: list[int] = []

    async def _refresh() -> None:
        calls.append(1)
        raise RuntimeError("boom")

    sched.attach_roster_refresh(_refresh)
    await sched._tick_once()
    await sched._tick_once()
    assert calls == [1, 1]
    assert sync_manager.reap_stale.call_count == 2


# ── v_46 authority epoch echo ─────────────────────────────────────────────


def _echo_sched(bus, queue, sync_manager, fed, *, spaces=None, members=None):
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([_peer("owner-o")]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type=spaces or {}, members_by_space=members or {}
        ),
        queue=queue,
        sync_manager=sync_manager,
        own_instance_id="self",
    )
    calls: list[tuple[str, str]] = []

    async def _echo(space_id, to):
        calls.append((space_id, to))
        return {"key_epoch": 7} if to == "owner-o" else None

    sched.attach_authority_echo(_echo)
    sched.wire()
    return sched, calls


async def test_begin_to_the_owner_carries_the_authority_echo(bus, queue, sync_manager):
    fed = _FakeFederation(confirmed={"owner-o"})
    sched, calls = _echo_sched(bus, queue, sync_manager, fed)
    await queue.start()
    try:
        await sched.enqueue_sync_for_space(space_id="sp-1", peer_instance_id="owner-o")
        await sched.enqueue_sync_for_space(space_id="sp-1", peer_instance_id="peer-b")
        for _ in range(50):
            if len(fed.sent) == 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await queue.stop()
    by_to = {m["to"]: m["payload"] for m in fed.sent}
    assert by_to["owner-o"]["authority_epoch_echo"] == {"key_epoch": 7}
    assert "authority_epoch_echo" not in by_to["peer-b"]
    assert ("sp-1", "owner-o") in calls


async def test_echo_builder_failure_never_blocks_the_begin(bus, queue, sync_manager):
    fed = _FakeFederation(confirmed={"owner-o"})
    sched, _ = _echo_sched(bus, queue, sync_manager, fed)

    async def _boom(space_id, to):
        raise RuntimeError("boom")

    sched.attach_authority_echo(_boom)
    await queue.start()
    try:
        await sched.enqueue_sync_for_space(space_id="sp-1", peer_instance_id="owner-o")
        for _ in range(50):
            if fed.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        await queue.stop()
    assert fed.sent and "authority_epoch_echo" not in fed.sent[0]["payload"]


async def test_mesh_catchup_carries_the_echo(bus, queue, sync_manager):
    fed = _FakeFederation()
    spaces = {SpaceType.HOUSEHOLD: [_space("sp-m", owner_instance_id="owner-o")]}
    sched, _ = _echo_sched(bus, queue, sync_manager, fed, spaces=spaces)
    await sched._tick_mesh_catchup()
    assert fed.mesh_catchups == [("sp-m", "owner-o")]
    assert fed.mesh_extras == [{"key_epoch": 7}]


async def test_echo_due_reaches_a_confirmed_owner_now(bus, queue, sync_manager):
    fed = _FakeFederation(confirmed={"owner-o"})
    _echo_sched(bus, queue, sync_manager, fed)
    await queue.start()
    try:
        await bus.publish(
            SpaceAuthorityEchoDue(space_id="sp-1", owner_instance_id="owner-o")
        )
        for _ in range(50):
            if fed.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        await queue.stop()
    assert fed.sent[0]["to"] == "owner-o"
    assert fed.sent[0]["type"] is FederationEventType.SPACE_SYNC_BEGIN
    assert fed.sent[0]["payload"]["authority_epoch_echo"] == {"key_epoch": 7}


async def test_echo_due_reaches_a_mesh_only_owner_over_the_mesh(
    bus, queue, sync_manager
):
    fed = _FakeFederation()
    _echo_sched(bus, queue, sync_manager, fed)
    await bus.publish(
        SpaceAuthorityEchoDue(space_id="sp-1", owner_instance_id="owner-o")
    )
    assert fed.mesh_catchups == [("sp-1", "owner-o")]
    assert fed.mesh_extras == [{"key_epoch": 7}]


async def test_echo_due_for_our_own_space_is_ignored(bus, queue, sync_manager):
    fed = _FakeFederation()
    _echo_sched(bus, queue, sync_manager, fed)
    await bus.publish(SpaceAuthorityEchoDue(space_id="sp-1", owner_instance_id="self"))
    assert fed.mesh_catchups == [] and fed.sent == []


async def test_no_echo_builder_means_no_echo(bus, queue, sync_manager):
    fed = _FakeFederation()
    spaces = {SpaceType.HOUSEHOLD: [_space("sp-m", owner_instance_id="owner-o")]}
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(spaces_by_type=spaces, members_by_space={}),
        queue=queue,
        sync_manager=sync_manager,
        own_instance_id="self",
    )
    await sched._tick_mesh_catchup()
    assert fed.mesh_extras == [None]


# ─── A provider at capacity: ask again (restart catch-up) ────────────


def _plain_sched(bus, queue, sync_manager, fed):
    return SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(spaces_by_type={}, members_by_space={}),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )


async def test_a_deferred_sync_is_asked_again(bus, queue, sync_manager, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "DEFERRED_SYNC_RETRY_S", 0.01)
    fed = _FakeFederation(confirmed={"host"})
    sched = _plain_sched(bus, queue, sync_manager, fed)
    sched.wire()
    await queue.start()
    try:
        await bus.publish(SpaceSyncDeferred(space_id="sp", provider_instance_id="host"))
        for _ in range(100):
            if fed.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        await sched.stop()
        await queue.stop()
    (begin,) = fed.sent
    assert begin["type"] is FederationEventType.SPACE_SYNC_BEGIN
    assert (begin["to"], begin["payload"]["space_id"]) == ("host", "sp")


async def test_deferred_retries_are_capped_and_reset_on_completion(
    bus, queue, sync_manager, monkeypatch
):
    monkeypatch.setattr(scheduler_mod, "DEFERRED_SYNC_RETRY_S", 0.0)
    fed = _FakeFederation(confirmed={"host"})
    sched = _plain_sched(bus, queue, sync_manager, fed)
    sched.wire()
    await queue.start()
    try:
        for _ in range(scheduler_mod.MAX_DEFERRED_SYNC_RETRIES + 3):
            await bus.publish(
                SpaceSyncDeferred(space_id="sp", provider_instance_id="host")
            )
            await asyncio.sleep(0.02)
        assert len(fed.sent) == scheduler_mod.MAX_DEFERRED_SYNC_RETRIES
        await bus.publish(SpaceSyncComplete(space_id="sp", from_instance="host"))
        await bus.publish(SpaceSyncDeferred(space_id="sp", provider_instance_id="host"))
        await asyncio.sleep(0.05)
        assert len(fed.sent) == scheduler_mod.MAX_DEFERRED_SYNC_RETRIES + 1
    finally:
        await sched.stop()
        await queue.stop()


async def test_a_deferred_sync_waits_on_stop(bus, queue, sync_manager, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "DEFERRED_SYNC_RETRY_S", 60.0)
    fed = _FakeFederation(confirmed={"host"})
    sched = _plain_sched(bus, queue, sync_manager, fed)
    sched.wire()
    await bus.publish(SpaceSyncDeferred(space_id="sp", provider_instance_id="host"))
    await asyncio.wait_for(sched.stop(), timeout=2.0)
    assert fed.sent == []
