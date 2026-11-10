"""Unit tests for :class:`SpaceSyncScheduler`."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialhome.domain.events import (
    PairingConfirmed,
    SpaceAuthorityEchoDue,
    SpaceInviteAnswered,
    SpaceSyncComplete,
    SpaceSyncDeferred,
)
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
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
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo


class _FakeFederation:
    def __init__(self, *, confirmed: set[str] | None = None):
        #: Instance ids seated from an invite link (relay-only).
        self.space_session: set[str] = set()
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
        self.requests: list[tuple] = []
        #: The ``extra_payload`` (v_46 authority echo) of each catch-up.
        self.mesh_extras: list[dict | None] = []
        #: Further BEGIN fields per mesh BEGIN (``sync_mode``, ``dir_seq``).
        self.mesh_fields: list[dict] = []

    def record_sync_request(
        self,
        *,
        sync_id,
        space_id,
        provider_instance_id,
        sync_mode="initial",
        have_seq=None,
    ):
        """A requester notes the sync_id it is about to ask for, so the
        provider's SPACE_SYNC_OFFER can be recognised as an answer."""
        self.requests.append(
            (sync_id, space_id, provider_instance_id, sync_mode, have_seq)
        )

    async def is_confirmed_peer(self, instance_id: str) -> bool:
        return instance_id in self.confirmed

    async def is_space_session_peer(self, instance_id: str) -> bool:
        return instance_id in self.space_session

    async def begin_mesh_catchup_sync(
        self, *, space_id, host_instance_id, extra_payload=None, **fields
    ):
        self.mesh_catchups.append((space_id, host_instance_id))
        self.mesh_extras.append(extra_payload)
        self.mesh_fields.append(fields)
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
        #: Ids whose row is gone (unpaired + purged) — ``get_instance``
        #: answers ``None`` for them; any other id reads as a known peer.
        self.gone: set[str] = set()

    async def get_instance(self, instance_id):
        if instance_id in self.gone:
            return None
        return SimpleNamespace(id=instance_id, status=PairingStatus.CONFIRMED)

    async def list_social_instances(self):
        return await self.list_instances(status="confirmed")

    async def list_instances(self, *, source=None, status=None):
        return self._instances


class _FakeSpaceRepo:
    def __init__(self, *, spaces_by_type, members_by_space, local_roles=None):
        self._spaces = spaces_by_type
        self._members = members_by_space
        #: ``space_id -> [role, …]`` of OUR local seats. A space not listed
        #: has one local ``member`` seat (the ordinary joined space).
        self.local_roles: dict[str, list[str]] = dict(local_roles or {})
        #: ``space_id -> [role, …]`` of the HOST's people the members sync
        #: mirrored into our ``space_members`` (no local ``users`` row).
        self.mirrored_roles: dict[str, list[str]] = {}

    async def list_by_type(self, space_type):
        return self._spaces.get(space_type, [])

    async def get(self, space_id):
        for spaces in self._spaces.values():
            for space in spaces:
                if space.id == space_id:
                    return space
        return None

    async def list_member_instances(self, space_id):
        return self._members.get(space_id, [])

    async def list_members(self, space_id):
        roles = self.local_roles.get(space_id, [SpaceRole.MEMBER.value])
        return [
            SimpleNamespace(space_id=space_id, user_id=f"u{i}", role=r)
            for i, r in enumerate(roles)
        ] + [
            SimpleNamespace(space_id=space_id, user_id=f"host-u{i}", role=r)
            for i, r in enumerate(self.mirrored_roles.get(space_id, []))
        ]

    async def list_local_member_user_ids(self, space_id):
        roles = self.local_roles.get(space_id, [SpaceRole.MEMBER.value])
        return [f"u{i}" for i in range(len(roles))]


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


def _peer(
    instance_id: str,
    *,
    source: InstanceSource = InstanceSource.MANUAL,
    relay_via: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=instance_id,
        status=PairingStatus.CONFIRMED,
        source=source,
        relay_via=relay_via,
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
    # #2(d): we take a quiet stream over the relay, without WebRTC.
    assert fed.sent[0]["payload"]["quiet_relay"] is True


async def test_queued_begin_for_a_peer_unpaired_meanwhile_is_skipped(
    bus, queue, sync_manager
):
    """The queue runs a task after it was enqueued. A household unpaired
    (row purged) in between gets no BEGIN — ``send_event`` could only drop
    it as an unknown instance at WARNING (federation demo, unpair-offline)
    — and no pending request is recorded for it."""
    fed = _FakeFederation()
    fed_repo = _FakeFedRepo([])
    fed_repo.gone.add("peer-a")
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=fed_repo,
        space_repo=_FakeSpaceRepo(spaces_by_type={}, members_by_space={}),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    await queue.start()
    try:
        await sched.enqueue_sync_for_space(space_id="sp-1", peer_instance_id="peer-a")
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    assert fed.sent == []
    assert fed.requests == []


class _Applied:
    """``AbstractSpaceSyncWatermarkRepo.applied_seq`` stand-in."""

    def __init__(self, seqs: dict[tuple[str, str], int], *, fail: bool = False):
        self._seqs = seqs
        self._fail = fail

    async def applied_seq(self, space_id: str, instance_id: str) -> int | None:
        if self._fail:
            raise RuntimeError("database is locked")
        return self._seqs.get((space_id, instance_id))


def _echo_scheduler(bus, queue, sync_manager, applied) -> SpaceSyncScheduler:
    return SpaceSyncScheduler(
        bus=bus,
        federation=_FakeFederation(),
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(spaces_by_type={}, members_by_space={}),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
        applied_seqs=applied,
    )


async def test_a_periodic_begin_echoes_the_last_cleanly_applied_snapshot(
    bus, queue, sync_manager
):
    """§25.6 (migration 0087): the provider streams since
    ``min(watermark, have_seq)``, so a rolled-back requester heals."""
    sched = _echo_scheduler(
        bus, queue, sync_manager, _Applied({("sp-1", "peer-a"): 41})
    )
    await queue.start()
    try:
        await sched.enqueue_sync_for_space(
            space_id="sp-1", peer_instance_id="peer-a", sync_mode="incremental"
        )
        await sched.enqueue_sync_for_space(
            space_id="sp-1", peer_instance_id="peer-b", sync_mode="incremental"
        )
        await sched.enqueue_sync_for_space(space_id="sp-1", peer_instance_id="peer-a")
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    # What each BEGIN asked for is noted with the request (the relay retry
    # re-sends it).
    assert {(r[2], r[3], r[4]) for r in sched._federation.requests} == {
        ("peer-a", "incremental", 41),
        ("peer-b", "incremental", None),
        ("peer-a", "initial", None),
    }
    sent = [(m["to"], m["payload"]) for m in sched._federation.sent]
    by_mode = {(to, p["sync_mode"]): p for to, p in sent}
    assert by_mode[("peer-a", "incremental")]["have_seq"] == 41
    # Nothing applied from peer-b yet: no echo, so it streams in full.
    assert "have_seq" not in by_mode[("peer-b", "incremental")]
    # A full session asks for everything anyway.
    assert "have_seq" not in by_mode[("peer-a", "initial")]


async def test_begin_fields_without_a_store_or_on_failure_carry_no_echo(
    bus, queue, sync_manager
):
    for applied in (None, _Applied({}, fail=True)):
        sched = _echo_scheduler(bus, queue, sync_manager, applied)
        assert (
            await sched.begin_fields(
                space_id="sp-1", peer_instance_id="peer-a", sync_mode="incremental"
            )
            == {}
        )


async def test_a_link_joined_provider_is_asked_for_relay_mode(bus, queue, sync_manager):
    """A household seated from an invite link is relay-only: asking it for
    a direct sync would make it send SPACE_SYNC_OFFER, which our own
    peer-class step refuses from a space-scoped peer."""
    fed = _FakeFederation()
    fed.space_session = {"link-peer"}
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(spaces_by_type={}, members_by_space={}),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    await queue.start()
    try:
        await sched.enqueue_sync_for_space(
            space_id="sp-1", peer_instance_id="link-peer"
        )
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    assert fed.sent[0]["payload"]["prefer_direct"] is False


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
    # The periodic re-sync is ``incremental``: the provider re-streams the
    # rows but does not re-ship every media blob every 30 minutes.
    assert {s["payload"]["sync_mode"] for s in beg} == {"incremental"}


async def test_the_tick_heals_a_gfs_seat_but_never_syncs_a_directory_seat(
    bus, queue, sync_manager
):
    """v_89. A frame to a household's GFS mailbox can be lost (the GFS
    queue's tail-drop or TTL while it was offline, a mailbox the GFS forgot):
    the 30-min incremental §25.6 sync between the host and its GFS-reached
    member (a ``space_session`` seat WITH a relay) restores it. A member
    seated from the host's directory (no relay of its own) gets live
    fan-out only — syncing every member with every GFS-reached member would
    be N² streams through the GFS every tick."""
    fed = _FakeFederation()
    spaces = {SpaceType.HOUSEHOLD: [_space("sp-1")]}
    members = {"sp-1": ["joiner", "directory-member"]}
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo(
            [
                _peer(
                    "joiner",
                    source=InstanceSource.SPACE_SESSION,
                    relay_via="https://gfs.example",
                ),
                _peer("directory-member", source=InstanceSource.SPACE_SESSION),
            ]
        ),
        space_repo=_FakeSpaceRepo(spaces_by_type=spaces, members_by_space=members),
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
    assert [(s["to"], s["payload"]["sync_mode"]) for s in beg] == [
        ("joiner", "incremental")
    ]


async def test_a_begin_echoes_the_member_directory_version(bus, queue, sync_manager):
    """v_89 review round 2: the periodic BEGIN to a space's host carries the
    ``dir_seq`` of the member directory we hold, so a host that is ahead
    repairs a lost delta with one snapshot."""
    fed = _FakeFederation()
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([_peer("host")]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type={SpaceType.HOUSEHOLD: [_space("sp-1")]},
            members_by_space={"sp-1": ["host"]},
        ),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )
    asked: list[tuple[str, str]] = []

    async def echo(space_id, peer):
        asked.append((space_id, peer))
        return 7

    sched.attach_directory_echo(echo)
    await queue.start()
    try:
        await sched._tick_once()
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    (begin,) = [
        s for s in fed.sent if s["type"] == FederationEventType.SPACE_SYNC_BEGIN
    ]
    assert begin["payload"]["dir_seq"] == 7
    assert asked == [("sp-1", "host")]


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


async def test_a_mesh_only_host_hears_our_directory_version_after_catch_up(
    bus, queue, sync_manager
):
    """v_89 review round 3: a removal delta lost to a member that reaches
    the host only over the mesh is repaired the same way as for a paired
    member — the catch-up BEGIN echoes our ``dir_seq``, and once catch-up is
    complete the periodic tick still sends a small incremental BEGIN with
    it (only for a space whose directory we hold), so a host that is ahead
    answers with one snapshot."""
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [
            _space("sp-mesh", owner_instance_id="host-x"),
            _space("sp-plain", owner_instance_id="host-x"),
        ],
    )

    async def echo(space_id, peer):
        return 41 if space_id == "sp-mesh" else None

    sched.attach_directory_echo(echo)
    await sched._tick_once()
    by_space = dict(zip([c[0] for c in fed.mesh_catchups], fed.mesh_fields))
    assert by_space["sp-mesh"]["dir_seq"] == 41
    assert by_space["sp-plain"].get("dir_seq") is None
    # Catch-up complete for both: only the space with a directory keeps
    # echoing, as an incremental BEGIN.
    sched._mesh_catchup_done.update({("sp-mesh", "host-x"), ("sp-plain", "host-x")})
    fed.mesh_catchups.clear()
    fed.mesh_fields.clear()
    await sched._tick_once()
    assert fed.mesh_catchups == [("sp-mesh", "host-x")]
    assert fed.mesh_fields == [
        {"dir_seq": 41, "sync_mode": "incremental", "have_seq": None}
    ]


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


async def test_begin_carries_the_roster_echo_its_builder_returns(
    bus, queue, sync_manager
):
    """The host stops resending a roster we hold once our BEGIN echoes it;
    a builder failure never costs the BEGIN."""
    fed = _FakeFederation(confirmed={"owner-o"})
    sched, _ = _echo_sched(bus, queue, sync_manager, fed)

    async def _roster_echo(space_id, to):
        if space_id == "sp-boom":
            raise RuntimeError("boom")
        return {"roster_version": 9, "cert_epoch": None} if to == "owner-o" else None

    sched.attach_roster_echo(_roster_echo)
    await queue.start()
    try:
        for space_id, to in (
            ("sp-1", "owner-o"),
            ("sp-1", "peer-b"),
            ("sp-boom", "owner-o"),
        ):
            await sched.enqueue_sync_for_space(space_id=space_id, peer_instance_id=to)
        for _ in range(50):
            if len(fed.sent) == 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await queue.stop()
    by_key = {(m["payload"]["space_id"], m["to"]): m["payload"] for m in fed.sent}
    assert by_key[("sp-1", "owner-o")]["roster_echo"] == {
        "roster_version": 9,
        "cert_epoch": None,
    }
    assert "roster_echo" not in by_key[("sp-1", "peer-b")]
    assert "roster_echo" not in by_key[("sp-boom", "owner-o")]


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


async def test_a_deferred_sync_keeps_its_mode(bus, queue, sync_manager, monkeypatch):
    """A refused periodic BEGIN is asked again as a periodic one — not as
    an ``"initial"`` full stream that re-ships every media blob."""
    monkeypatch.setattr(scheduler_mod, "DEFERRED_SYNC_RETRY_S", 0.01)
    fed = _FakeFederation(confirmed={"host"})
    sched = _plain_sched(bus, queue, sync_manager, fed)
    sched.wire()
    await queue.start()
    try:
        await bus.publish(
            SpaceSyncDeferred(
                space_id="sp",
                provider_instance_id="host",
                sync_mode="incremental",
                sync_id="s1",
            )
        )
        for _ in range(100):
            if fed.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        await sched.stop()
        await queue.stop()
    (begin,) = fed.sent
    assert begin["payload"]["sync_mode"] == "incremental"


def _many_spaces_sched(bus, queue, sync_manager, fed, n: int):
    spaces = {SpaceType.HOUSEHOLD: [_space(f"sp-{i}") for i in range(n)]}
    members = {f"sp-{i}": ["host"] for i in range(n)}
    return SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([_peer("host")]),
        space_repo=_FakeSpaceRepo(spaces_by_type=spaces, members_by_space=members),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
    )


async def _settle(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0.01)


def _begins(fed) -> list[dict]:
    return [s for s in fed.sent if s["type"] == FederationEventType.SPACE_SYNC_BEGIN]


async def test_periodic_tick_asks_one_provider_for_at_most_its_capacity(
    bus, queue, sync_manager
):
    """8 shared spaces with one host: the tick used to fire 8 BEGINs at
    once; the host serves 3 and refuses 5, and each refusal came back as
    a full re-sync with media. Now 3 go out and the rest wait for a slot."""
    fed = _FakeFederation(confirmed={"host"})
    sched = _many_spaces_sched(bus, queue, sync_manager, fed, 8)
    sched.wire()
    await queue.start()
    try:
        await sched._tick_once()
        await _settle()
        first = _begins(fed)
        assert len(first) == scheduler_mod.MAX_ACTIVE_SESSIONS_PER_INSTANCE == 3
        # One session ends → the next space is asked for, still periodic.
        done = first[0]["payload"]
        await bus.publish(
            SpaceSyncComplete(
                space_id=done["space_id"], from_instance="host", sync_id=done["sync_id"]
            )
        )
        await _settle()
        after = _begins(fed)
        assert len(after) == 4
        assert after[-1]["payload"]["sync_mode"] == "incremental"
        assert len({b["payload"]["space_id"] for b in after}) == 4
    finally:
        await sched.stop()
        await queue.stop()


async def test_every_shared_space_is_asked_for_once_slots_free(
    bus, queue, sync_manager
):
    fed = _FakeFederation(confirmed={"host"})
    sched = _many_spaces_sched(bus, queue, sync_manager, fed, 8)
    sched.wire()
    await queue.start()
    try:
        await sched._tick_once()
        for _ in range(10):
            await _settle(5)
            for b in _begins(fed):
                p = b["payload"]
                await bus.publish(
                    SpaceSyncComplete(
                        space_id=p["space_id"],
                        from_instance="host",
                        sync_id=p["sync_id"],
                    )
                )
        await _settle()
    finally:
        await sched.stop()
        await queue.stop()
    spaces = [b["payload"]["space_id"] for b in _begins(fed)]
    assert sorted(spaces) == sorted(f"sp-{i}" for i in range(8))


async def test_a_slot_that_never_completes_is_reclaimed(bus, queue, sync_manager):
    """A session whose end never reaches us must not wedge the provider:
    after the in-flight TTL the waiting spaces are asked for anyway (a
    still-busy host answers ``too_many_sessions`` and the deferral retries)."""
    # No wall-clock race: the TTL is far away while the first three go
    # out (it used to be 50 ms — the same as the settle window, so a slow
    # runner reclaimed slots before the first assertion). The slots are
    # then aged past it by hand, and the drain the timer runs is run.
    fed = _FakeFederation(confirmed={"host"})
    sched = _many_spaces_sched(bus, queue, sync_manager, fed, 5)
    sched.wire()
    await queue.start()
    try:
        await sched._tick_once()
        await _settle(5)
        assert len(_begins(fed)) == 3
        # A timer is armed to reclaim them.
        assert not sched._drain_timers["host"].done()
        held = sched._inflight["host"]
        for sync_id, (space_id, _at) in list(held.items()):
            held[sync_id] = (
                space_id,
                scheduler_mod.time.monotonic() - scheduler_mod.INFLIGHT_BEGIN_TTL_S - 1,
            )
        await sched._drain_parked("host")
        await _settle(5)
        assert len(_begins(fed)) == 5
    finally:
        await sched.stop()
        await queue.stop()


async def test_a_waiting_space_is_asked_for_once_with_the_strongest_mode(
    bus, queue, sync_manager
):
    """Two ticks while the host is busy queue the same space once; a
    "Sync now" (``initial``) for it wins over the periodic ask."""
    fed = _FakeFederation(confirmed={"host"})
    sched = _many_spaces_sched(bus, queue, sync_manager, fed, 4)
    sched.wire()
    await queue.start()
    try:
        await sched._tick_once()
        await _settle(5)
        await sched._tick_once()
        await sched.enqueue_sync_for_space(space_id="sp-3", peer_instance_id="host")
        await _settle(5)
        begins = _begins(fed)
        assert len(begins) == 3
        p = begins[0]["payload"]
        await bus.publish(
            SpaceSyncComplete(
                space_id=p["space_id"], from_instance="host", sync_id=p["sync_id"]
            )
        )
        await _settle()
        begins = _begins(fed)
        waiting = [b["payload"] for b in begins[3:]]
        assert [(w["space_id"], w["sync_mode"]) for w in waiting] == [
            ("sp-3", "initial")
        ]
    finally:
        await sched.stop()
        await queue.stop()


async def test_a_deferral_frees_the_slot_for_its_own_retry(
    bus, queue, sync_manager, monkeypatch
):
    """A refused BEGIN held no session at the host: its slot is free for
    its own retry — but a refusal never pulls the next waiting space in
    (the host just said it is full; that would only be refused too)."""
    monkeypatch.setattr(scheduler_mod, "DEFERRED_SYNC_RETRY_S", 0.01)
    fed = _FakeFederation(confirmed={"host"})
    sched = _many_spaces_sched(bus, queue, sync_manager, fed, 4)
    sched.wire()
    await queue.start()
    try:
        await sched._tick_once()
        await _settle(5)
        p = _begins(fed)[0]["payload"]
        await bus.publish(
            SpaceSyncDeferred(
                space_id=p["space_id"],
                provider_instance_id="host",
                sync_mode="incremental",
                sync_id=p["sync_id"],
            )
        )
        await _settle()
        begins = _begins(fed)
        assert len(begins) == 4
        assert begins[-1]["payload"]["space_id"] == p["space_id"]
        assert begins[-1]["payload"]["sync_mode"] == "incremental"
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


# ── Mesh-only host version announcement (migration 0078) ──────────────────


async def test_tick_announces_our_version_once_to_each_mesh_only_host(
    bus, queue, sync_manager
):
    """A host we reach only over the mesh holds no row for us, so it can
    gate nothing on our version. The sweep hands each such host (once per
    process, retried until delivered) to the attached announcer; confirmed
    hosts and our own spaces are skipped."""
    fed = _FakeFederation(confirmed={"host-paired"})
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [
            _space("sp-1", owner_instance_id="host-x"),
            _space("sp-2", owner_instance_id="host-x"),
            _space("sp-3", owner_instance_id="host-paired"),
            _space("sp-4", owner_instance_id="self"),
        ],
    )
    announced: list[str] = []
    outcome = {"ok": False}

    async def _announce(host: str) -> bool:
        announced.append(host)
        return outcome["ok"]

    sched.attach_mesh_announce(_announce)
    assert await sched._tick_mesh_announce() == 1
    assert announced == ["host-x"]
    # Not delivered → retried on the next tick; delivered → never again.
    outcome["ok"] = True
    await sched._tick_once()
    assert announced == ["host-x", "host-x"]
    await sched._tick_once()
    assert await sched._tick_mesh_announce() == 0
    assert announced == ["host-x", "host-x"]


async def test_mesh_announce_failure_does_not_kill_the_tick(bus, queue, sync_manager):
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus, queue, sync_manager, fed, [_space("sp-1", owner_instance_id="host-x")]
    )

    async def _boom(host: str) -> bool:
        raise RuntimeError("mesh down")

    sched.attach_mesh_announce(_boom)
    assert await sched._tick_mesh_announce() == 1
    await sched._tick_once()
    sync_manager.reap_stale.assert_called_once_with(STALE_SESSION_TTL_SECONDS)


async def test_startup_sweep_retries_an_undelivered_announcement(
    bus, queue, sync_manager
):
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus, queue, sync_manager, fed, [_space("sp-1", owner_instance_id="host-x")]
    )
    sched.wire()
    # Catch-up already complete: only the announcement keeps the sweep alive.
    await bus.publish(SpaceSyncComplete(space_id="sp-1", from_instance="host-x"))
    calls: list[str] = []

    async def _announce(host: str) -> bool:
        calls.append(host)
        return len(calls) >= 2

    sched.attach_mesh_announce(_announce)
    with (
        patch.object(scheduler_mod, "STARTUP_MESH_CATCHUP_DELAY_SECONDS", 0.01),
        patch.object(
            scheduler_mod, "STARTUP_MESH_CATCHUP_RETRY_DELAYS_S", (0.01, 0.01, 0.01)
        ),
    ):
        await sched._startup_mesh_catchup()
    assert calls == ["host-x", "host-x"]


# ── Followed-only spaces stay invisible to their host ─────────────────────


@pytest.mark.parametrize(
    ("roles", "mirrored"),
    [
        # we only follow it (GFS subscriber seat)
        ([SpaceRole.SUBSCRIBER.value], []),
        ([], []),  # a bare GFS mirror / directory stub, no seat at all
        # Only the host's people, left mirrored by an earlier members sync
        # in ``space_members``: none of them is a seat of ours.
        ([SpaceRole.SUBSCRIBER.value], [SpaceRole.OWNER.value]),
        ([], [SpaceRole.OWNER.value, SpaceRole.MEMBER.value]),
    ],
)
async def test_a_followed_only_space_host_gets_no_announce_and_no_catchup(
    bus, queue, sync_manager, roles, mirrored
):
    """The GFS shields followers from hosts: a PUBLIC space we only follow
    (or merely mirror) must never make us route-discover its host, announce
    our version to it or ask it for a catch-up — that would tell the host
    we exist and are interested."""
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-followed", owner_instance_id="host-f")],
    )
    sched._space_repo.local_roles["sp-followed"] = roles
    sched._space_repo.mirrored_roles["sp-followed"] = mirrored
    announced: list[str] = []

    async def _announce(host: str) -> bool:
        announced.append(host)
        return True

    sched.attach_mesh_announce(_announce)
    await sched._tick_once()
    assert await sched._tick_mesh_catchup() == 0
    assert await sched._tick_mesh_announce() == 0
    assert announced == []
    assert fed.mesh_catchups == []


async def test_a_member_space_host_still_gets_announce_and_catchup(
    bus, queue, sync_manager
):
    """A household holding a real seat (here alongside a follower seat of
    another local user) keeps both sweeps."""
    fed = _FakeFederation(confirmed=set())
    sched = _mesh_sched(
        bus,
        queue,
        sync_manager,
        fed,
        [_space("sp-member", owner_instance_id="host-m")],
    )
    sched._space_repo.local_roles["sp-member"] = [
        SpaceRole.SUBSCRIBER.value,
        SpaceRole.MEMBER.value,
    ]
    announced: list[str] = []

    async def _announce(host: str) -> bool:
        announced.append(host)
        return True

    sched.attach_mesh_announce(_announce)
    await sched._tick_once()
    assert announced == ["host-m"]
    assert fed.mesh_catchups == [("sp-member", "host-m")]


# ── Migration 0093: the trusted inviting admin is a mesh target too ───────


@pytest.mark.parametrize("delegation", [True, False])
async def test_mesh_catchup_also_asks_the_trusted_inviting_admin(db, delegation):
    """A household a delegated admin invited may be unknown to the owner (or
    the owner offline): the catch-up also goes to the inviting admin while it
    is host-equivalent here — never once delegation is off. The authority
    echo stays the owner's."""
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('joe','u-joe','J')"
    )
    await spaces.save(
        Space(
            id="sp-inv",
            name="S",
            owner_instance_id="owner-hh",
            owner_username="o",
            identity_public_key="aa" * 32,
            config_sequence=1,
            features=SpaceFeatures(delegated_admin_authority=delegation),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    await spaces.save_member(
        SpaceMember(space_id="sp-inv", user_id="u-joe", role="member", joined_at="t")
    )
    await seats.add(
        space_id="sp-inv",
        instance_id="admin-hh",
        user_id="u-admin",
        user_pk=None,
        display_name="A",
        role=SpaceRole.ADMIN.value,
    )
    await spaces.set_invited_by("sp-inv", "admin-hh", 0)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    await spaces.set_owner_statement(
        "sp-inv",
        json.dumps(
            {"authority_pk": "aa" * 32, "authority_key_epoch": 0, "issued_at": now_ms}
        ),
    )
    fed = MagicMock()
    fed.is_confirmed_peer = AsyncMock(return_value=False)
    fed.begin_mesh_catchup_sync = AsyncMock(return_value=True)
    sched = SpaceSyncScheduler(
        bus=EventBus(),
        federation=fed,
        federation_repo=MagicMock(),
        space_repo=spaces,
        queue=MagicMock(),
        own_instance_id="self",
        sync_manager=MagicMock(),
        seat_repo=seats,
    )
    await sched._tick_mesh_catchup()
    calls = {
        c.kwargs["host_instance_id"]: c.kwargs.get("extra_payload")
        for c in fed.begin_mesh_catchup_sync.await_args_list
    }
    expected = {"owner-hh", "admin-hh"} if delegation else {"owner-hh"}
    assert set(calls) == expected
    if delegation:
        assert calls["admin-hh"] is None


# ─── Joined a paired household's space: ask now (trigger 9) ──────────


def _join_sched(bus, queue, sync_manager, fed, *, owner="host", applied=None):
    return SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type={
                SpaceType.HOUSEHOLD: [_space("sp", owner_instance_id=owner)]
            },
            members_by_space={"sp": [owner]},
        ),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
        applied_seqs=applied,
    )


async def _answered(bus, queue, *, accepted=True, space_id="sp"):
    await queue.start()
    try:
        await bus.publish(
            SpaceInviteAnswered(space_id=space_id, user_id="oma", accepted=accepted)
        )
        await _settle()
    finally:
        await queue.stop()


async def test_joining_a_paired_hosts_space_asks_for_it_now(bus, queue, sync_manager):
    """A new member household used to hold an empty space (no albums, so a
    photo shared into one was refused) until its next periodic tick."""
    fed = _FakeFederation(confirmed={"host"})
    sched = _join_sched(bus, queue, sync_manager, fed, applied=_Applied({}))
    sched.wire()
    await _answered(bus, queue)
    (begin,) = _begins(fed)
    assert (begin["to"], begin["payload"]["space_id"]) == ("host", "sp")
    assert begin["payload"]["sync_mode"] == "initial"
    assert fed.mesh_catchups == []


async def test_joining_asks_nothing_once_a_stream_applied_cleanly(
    bus, queue, sync_manager
):
    """A second person of a household already in the space: its copy is
    current, the periodic tick keeps it so."""
    fed = _FakeFederation(confirmed={"host"})
    sched = _join_sched(
        bus, queue, sync_manager, fed, applied=_Applied({("sp", "host"): 7})
    )
    sched.wire()
    await _answered(bus, queue)
    assert _begins(fed) == []


@pytest.mark.parametrize(
    ("owner", "confirmed", "accepted", "space_id"),
    [
        ("host", {"host"}, False, "sp"),  # declined
        ("self", {"host"}, True, "sp"),  # our own space
        ("host", set(), True, "sp"),  # mesh-only host: the accept's own catch-up
        ("host", {"host"}, True, "unknown"),  # no such space here
    ],
)
async def test_joining_asks_nothing_when_there_is_no_paired_host(
    bus, queue, sync_manager, owner, confirmed, accepted, space_id
):
    fed = _FakeFederation(confirmed=confirmed)
    sched = _join_sched(bus, queue, sync_manager, fed, owner=owner)
    sched.wire()
    await _answered(bus, queue, accepted=accepted, space_id=space_id)
    assert _begins(fed) == []
    assert fed.mesh_catchups == []


async def test_joining_survives_a_failing_lookup(bus, queue, sync_manager, caplog):
    fed = _FakeFederation(confirmed={"host"})
    sched = _join_sched(bus, queue, sync_manager, fed, applied=_Applied({}, fail=True))
    sched.wire()
    await _answered(bus, queue)
    assert _begins(fed) == []
    assert "could not be asked for" in caplog.text


async def test_an_interrupted_stream_is_asked_again_in_its_mode(
    bus, queue, sync_manager, monkeypatch
):
    """The DataChannel closed before the sentinel (the host restarted): the
    deferred retry asks again, as the cut stream was asked for."""
    monkeypatch.setattr(scheduler_mod, "DEFERRED_SYNC_RETRY_S", 0.01)
    fed = _FakeFederation(confirmed={"host"})
    sched = _plain_sched(bus, queue, sync_manager, fed)
    sched.wire()
    await queue.start()
    try:
        await bus.publish(
            SpaceSyncDeferred(
                space_id="sp",
                provider_instance_id="host",
                sync_mode="initial",
                sync_id="cut",
                reason="interrupted",
            )
        )
        for _ in range(100):
            if fed.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        await sched.stop()
        await queue.stop()
    (begin,) = fed.sent
    assert (begin["to"], begin["payload"]["sync_mode"]) == ("host", "initial")


# ─── v_92: trusted helpers while the host is offline ─────────────────────


class _HelperSpaceRepo(_FakeSpaceRepo):
    async def get_invited_by(self, space_id):
        return None


class _Seats:
    """``list_instances_with_roles`` over a fixed roster mirror."""

    def __init__(self, helpers: list[str]) -> None:
        self.helpers = helpers
        self.asked: list[tuple[str, frozenset[str]]] = []

    async def list_instances_with_roles(self, space_id, roles):
        self.asked.append((space_id, frozenset(roles)))
        return list(self.helpers)


class _Resume:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def send_request(self, *, space_id, instance_id, since):
        self.requests.append(
            {"space_id": space_id, "instance_id": instance_id, "since": since}
        )


def _helper_sched(
    bus,
    queue,
    sync_manager,
    *,
    helpers: list[str],
    owner: str = "host",
    authority: bool = False,
):
    from socialhome.federation.sync.space.host_contact import HostContact

    fed = _FakeFederation(confirmed={"host"})
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([_peer("host")]),
        space_repo=_HelperSpaceRepo(
            spaces_by_type={
                SpaceType.PRIVATE: [_space("sp-1", owner_instance_id=owner)]
            },
            members_by_space={"sp-1": ["host"]},
        ),
        queue=queue,
        own_instance_id="self",
        sync_manager=sync_manager,
        seat_repo=_Seats(helpers),
    )
    contact = HostContact()
    resume = _Resume()

    async def _authority(space_id: str) -> bool:
        return authority

    sched.attach_helpers(
        host_contact=contact, resume=resume, roster_authority=_authority
    )
    return sched, contact, resume


async def _wait_checks(sched) -> None:
    await asyncio.gather(*list(sched._helper_checks.values()))


@pytest.fixture
def quick_answer_wait(monkeypatch):
    monkeypatch.setattr(scheduler_mod, "HOST_ANSWER_WAIT_S", 0.01)


async def test_a_silent_host_sends_one_helper_the_bounded_resume(
    bus, queue, sync_manager, quick_answer_wait
):
    """The tick asks the host first (its BEGIN); no answer within
    HOST_ANSWER_WAIT_S → ONE trusted helper (never us, never the host) gets
    SPACE_SYNC_RESUME since 30 days ago (nothing heard yet)."""
    from datetime import timedelta

    sched, _contact, resume = _helper_sched(
        bus, queue, sync_manager, helpers=["self", "host", "mod-b", "admin-a"]
    )
    await sched._tick_once()
    await _wait_checks(sched)
    [request] = resume.requests
    assert request["space_id"] == "sp-1"
    assert request["instance_id"] == "admin-a"
    floor = datetime.now(UTC) - timedelta(days=30)
    assert abs(datetime.fromisoformat(request["since"]) - floor) < timedelta(minutes=1)
    assert sched._seat_repo.asked == [
        ("sp-1", frozenset({SpaceRole.ADMIN.value, SpaceRole.MODERATOR.value}))
    ]


async def test_a_host_that_answered_gets_no_fallback(
    bus, queue, sync_manager, monkeypatch
):
    monkeypatch.setattr(scheduler_mod, "HOST_ANSWER_WAIT_S", 0.05)
    sched, contact, resume = _helper_sched(
        bus, queue, sync_manager, helpers=["admin-a"]
    )
    await sched._tick_once()
    contact.heard("sp-1")  # the receiver saw the host's sentinel
    await _wait_checks(sched)
    assert resume.requests == []


async def test_the_resume_asks_since_the_host_was_last_heard(
    bus, queue, sync_manager, quick_answer_wait
):
    sched, contact, resume = _helper_sched(
        bus, queue, sync_manager, helpers=["admin-a"]
    )
    contact.heard("sp-1")  # heard once, before this round's BEGIN
    last = contact.last_heard_iso("sp-1")
    await sched._tick_once()
    await _wait_checks(sched)
    assert [r["since"] for r in resume.requests] == [last]


async def test_the_helper_rotates_one_step_per_round(
    bus, queue, sync_manager, quick_answer_wait
):
    sched, _contact, resume = _helper_sched(
        bus, queue, sync_manager, helpers=["mod-b", "admin-a"]
    )
    for _ in range(3):
        await sched._tick_once()
        await _wait_checks(sched)
    assert [r["instance_id"] for r in resume.requests] == [
        "admin-a",
        "mod-b",
        "admin-a",
    ]


async def test_no_helper_means_nothing_is_sent(
    bus, queue, sync_manager, quick_answer_wait, caplog
):
    import logging

    sched, _contact, resume = _helper_sched(
        bus, queue, sync_manager, helpers=["self", "host"]
    )
    with caplog.at_level(logging.INFO, logger=scheduler_mod.__name__):
        await sched._tick_once()
        await _wait_checks(sched)
    assert resume.requests == []
    assert sum("no trusted helper" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize(("owner", "authority"), [("self", False), ("host", True)])
async def test_a_host_equivalent_household_never_falls_back(
    bus, queue, sync_manager, quick_answer_wait, owner, authority
):
    """We host the space, or hold its roster authority (a seed-holding
    delegated admin): nobody to fall back from."""
    sched, _contact, resume = _helper_sched(
        bus,
        queue,
        sync_manager,
        helpers=["admin-a"],
        owner=owner,
        authority=authority,
    )
    await sched._tick_once()
    await _wait_checks(sched)
    assert resume.requests == []
    assert sched._helper_checks == {}


async def test_a_round_without_a_begin_to_the_host_waits_for_nothing(
    bus, queue, sync_manager, quick_answer_wait
):
    """A mesh-only host whose catch-up completed is not asked every tick:
    no BEGIN this round, so no answer is due and no helper is asked."""
    sched, _contact, resume = _helper_sched(
        bus, queue, sync_manager, helpers=["admin-a"]
    )
    sched._federation.confirmed = set()
    sched._federation_repo = _FakeFedRepo([])
    sched._mesh_catchup_done.add(("sp-1", "host"))
    await sched._tick_once()
    await _wait_checks(sched)
    assert resume.requests == []


async def test_a_mesh_only_hosts_catch_up_begin_is_waited_for(
    bus, queue, sync_manager, quick_answer_wait
):
    sched, _contact, resume = _helper_sched(
        bus, queue, sync_manager, helpers=["admin-a"]
    )
    sched._federation.confirmed = set()
    sched._federation_repo = _FakeFedRepo([])
    await sched._tick_once()
    await _wait_checks(sched)
    assert sched._federation.mesh_catchups == [("sp-1", "host")]
    assert [r["instance_id"] for r in resume.requests] == ["admin-a"]


async def test_stop_ends_a_pending_check_without_a_fallback(
    bus, queue, sync_manager, monkeypatch
):
    monkeypatch.setattr(scheduler_mod, "HOST_ANSWER_WAIT_S", 30.0)
    sched, _contact, resume = _helper_sched(
        bus, queue, sync_manager, helpers=["admin-a"]
    )
    await sched._tick_once()
    assert len(sched._helper_checks) == 1
    await sched.stop()
    assert sched._helper_checks == {}
    assert resume.requests == []


async def test_one_check_per_space_at_a_time(bus, queue, sync_manager, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "HOST_ANSWER_WAIT_S", 0.05)
    sched, _contact, resume = _helper_sched(
        bus, queue, sync_manager, helpers=["admin-a"]
    )
    await sched._tick_once()
    await sched._tick_once()
    assert len(sched._helper_checks) == 1
    await _wait_checks(sched)
    assert len(resume.requests) == 1


async def test_a_hosts_capacity_deferral_counts_as_its_answer(bus, queue, sync_manager):
    sched, contact, _resume = _helper_sched(
        bus, queue, sync_manager, helpers=["admin-a"]
    )
    t0 = contact.now()
    await sched._on_sync_deferred(
        SpaceSyncDeferred(
            space_id="sp-1",
            provider_instance_id="admin-a",
            sync_mode="incremental",
            sync_id="s-1",
        )
    )
    assert not contact.heard_since("sp-1", t0)
    await sched._on_sync_deferred(
        SpaceSyncDeferred(
            space_id="sp-1",
            provider_instance_id="host",
            sync_mode="incremental",
            sync_id="s-2",
        )
    )
    assert contact.heard_since("sp-1", t0)
    await sched.stop()
