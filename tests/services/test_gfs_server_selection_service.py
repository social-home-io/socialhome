"""Tests for :class:`GfsServerSelectionService` — the owner's signed
``spaces.gfs_servers`` snapshot (migration 0093), derived from its active
GFS listings and handed to members in a sequence-raised config change."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import (
    derive_instance_id,
    ed25519_public_key,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpaceConfigChanged
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    GfsConnection,
)
from socialhome.domain.gfs_server import MAX_GFS_SERVERS, GfsServer
from socialhome.domain.errors import (
    GfsSelectionInvalidError,
    GfsSelectionRequiredError,
    SpaceNotPublishableError,
    TierChangeRequiredError,
)
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceConfigEventType,
    SpaceFeatures,
    SpaceMember,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.gfs_server_list_sig import (
    sign_gfs_server_list,
    verify_gfs_server_list,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteUserRepo,
)
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.federation_inbound_service import FederationInboundService
from socialhome.services.gfs_connection_service import GfsConnectionError
from socialhome.services.gfs_server_selection_service import (
    GfsServerSelectionService,
)
from socialhome.services.space_config_outbound import SpaceConfigOutbound

_SEED = b"\x11" * 32
_KP = SimpleNamespace(private_key=_SEED, public_key=ed25519_public_key(_SEED))
OWN = derive_instance_id(_KP.public_key)


class _Env:
    def __init__(self, db) -> None:
        self.db = db
        self.spaces = SqliteSpaceRepo(db)
        self.gfs = SqliteGfsConnectionRepo(db)
        self.bus = EventBus()
        self.events: list[SpaceConfigChanged] = []

        async def _record(event: SpaceConfigChanged) -> None:
            self.events.append(event)

        self.bus.subscribe(SpaceConfigChanged, _record)
        self.svc = GfsServerSelectionService(
            space_repo=self.spaces, gfs_repo=self.gfs, bus=self.bus
        )
        self.svc.attach_identity(own_instance_id=OWN, own_signing_key=_KP.private_key)

    async def space(
        self,
        space_id: str = "sp-1",
        *,
        owner: str = OWN,
        space_type: SpaceType = SpaceType.GLOBAL,
    ) -> None:
        await self.spaces.save(
            Space(
                id=space_id,
                name="S",
                owner_instance_id=owner,
                owner_username="anna",
                identity_public_key="aa" * 32,
                config_sequence=3,
                features=SpaceFeatures(),
                space_type=space_type,
                join_mode=JoinMode.OPEN,
            )
        )

    async def listed(
        self,
        gfs_id: str,
        space_id: str = "sp-1",
        *,
        status: str = "active",
        conn_status: str = "active",
        inbox_url: str | None = None,
        public_key: str | None = None,
    ) -> None:
        await self.gfs.save(
            GfsConnection(
                id=gfs_id,
                gfs_instance_id=f"inst-{gfs_id}",
                display_name=f"GFS {gfs_id}",
                public_key=public_key or (gfs_id.encode().hex() * 32)[:64],
                inbox_url=inbox_url or f"https://{gfs_id}.example",
                status=conn_status,
                paired_at="2026-10-01T00:00:00+00:00",
            )
        )
        await self.gfs.publish_space(space_id, gfs_id, status)

    async def listed_ids(self, space_id: str = "sp-1") -> list[str]:
        space = await self.spaces.get(space_id)
        assert space is not None and space.gfs_servers is not None
        servers, _ = space.gfs_servers.servers(
            own_routes={
                (c.gfs_instance_id, c.inbox_url) for c in await self.gfs.list_all()
            }
        )
        return [s.gfs_instance_id for s in servers]


@pytest.fixture
def env(db) -> _Env:
    return _Env(db)


async def test_snapshot_signs_the_active_listings_and_tells_members(env):
    await env.space()
    await env.listed("g2")
    await env.listed("g1")
    assert await env.svc.sync_snapshot("sp-1") is True

    space = await env.spaces.get("sp-1")
    assert space is not None and space.gfs_servers is not None
    signed = space.gfs_servers
    assert verify_gfs_server_list(signed, space_id="sp-1", owner_instance_id=OWN)
    servers, dropped = signed.servers()
    assert dropped == 0
    assert [s.gfs_instance_id for s in servers] == ["inst-g1", "inst-g2"]
    first = servers[0]
    assert first.inbox_url == "https://g1.example"
    assert first.gfs_key_suite == "ed25519"
    assert first.display_name == "GFS g1"
    # The sequence went up, so members apply the change.
    assert space.config_sequence == 4
    assert len(env.events) == 1
    event = env.events[0]
    assert event.space_id == "sp-1"
    assert event.event_type == SpaceConfigEventType.GFS_SERVERS_CHANGED.value
    assert event.sequence == 4


async def test_unchanged_snapshot_writes_and_sends_nothing(env):
    await env.space()
    await env.listed("g1")
    assert await env.svc.sync_snapshot("sp-1") is True
    first = (await env.spaces.get("sp-1")).gfs_servers
    assert await env.svc.sync_snapshot("sp-1") is False
    space = await env.spaces.get("sp-1")
    assert space is not None and space.config_sequence == 4
    assert space.gfs_servers == first
    assert len(env.events) == 1


async def test_issued_at_only_goes_up(env):
    """A list signed after a small clock step back still outranks the
    stored one."""
    await env.space()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ahead = sign_gfs_server_list(
        space_id="sp-1",
        servers=(),
        issued_at=now_ms + 60_000,
        owner_seed=_KP.private_key,
    )
    await env.spaces.set_gfs_servers("sp-1", ahead)
    await env.listed("g1")
    await env.svc.sync_snapshot("sp-1")
    space = await env.spaces.get("sp-1")
    assert space is not None and space.gfs_servers is not None
    assert space.gfs_servers.issued_at == now_ms + 60_001


async def test_a_runaway_stored_issued_at_does_not_wedge_the_owner(env, caplog):
    """Members refuse a list dated more than a day ahead, so the owner never
    signs one: a runaway stored value is answered with ``now``."""
    await env.space()
    await env.spaces.set_gfs_servers(
        "sp-1",
        sign_gfs_server_list(
            space_id="sp-1", servers=(), issued_at=2**60, owner_seed=_KP.private_key
        ),
    )
    await env.listed("g1")
    with caplog.at_level(logging.WARNING):
        await env.svc.sync_snapshot("sp-1")
    space = await env.spaces.get("sp-1")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    assert space is not None and space.gfs_servers is not None
    assert space.gfs_servers.issued_at <= now_ms
    assert "far ahead" in caplog.text


async def test_snapshot_locks_are_pruned(env):
    await env.space()
    await env.listed("g1")
    await asyncio.gather(*(env.svc.sync_snapshot("sp-1") for _ in range(3)))
    assert env.svc._locks == {}


async def test_pending_listing_and_suspended_connection_are_left_out(env):
    await env.space()
    await env.listed("g1")
    await env.listed("g2", status="pending")
    await env.listed("g3", conn_status="suspended")
    await env.svc.sync_snapshot("sp-1")
    assert await env.listed_ids() == ["inst-g1"]


async def test_listed_nowhere_is_a_signed_empty_list_not_unknown(env):
    await env.space()
    await env.listed("g1")
    await env.svc.sync_snapshot("sp-1")
    await env.gfs.unpublish_space("sp-1", "g1")
    assert await env.svc.sync_snapshot("sp-1") is True
    space = await env.spaces.get("sp-1")
    assert space is not None and space.gfs_servers is not None
    assert space.gfs_servers.servers() == ((), 0)
    assert len(env.events) == 2


async def test_a_connection_with_an_unusable_url_is_dropped_and_logged(env, caplog):
    await env.space()
    await env.listed("g1")
    await env.listed("g2", inbox_url="http://public.example")
    with caplog.at_level(logging.WARNING):
        await env.svc.sync_snapshot("sp-1")
    assert await env.listed_ids() == ["inst-g1"]
    assert "left out" in caplog.text


async def test_duplicate_url_or_key_is_listed_once(env):
    await env.space()
    await env.listed("g1", inbox_url="https://same.example")
    await env.listed("g2", inbox_url="https://same.example")
    await env.listed("g3", public_key="cd" * 32)
    await env.listed("g4", public_key="cd" * 32)
    await env.svc.sync_snapshot("sp-1")
    assert await env.listed_ids() == ["inst-g1", "inst-g3"]


async def test_list_is_capped(env, caplog):
    await env.space()
    for i in range(MAX_GFS_SERVERS + 2):
        await env.listed(f"g{i:02d}")
    with caplog.at_level(logging.WARNING):
        await env.svc.sync_snapshot("sp-1")
    assert len(await env.listed_ids()) == MAX_GFS_SERVERS
    assert caplog.text.count("members get the first") == 1


@pytest.mark.parametrize(
    "owner, space_type",
    [
        ("someone-else", SpaceType.GLOBAL),
        (OWN, SpaceType.PRIVATE),
        (OWN, SpaceType.HOUSEHOLD),
    ],
)
async def test_nothing_for_spaces_we_do_not_own_or_never_listed(env, owner, space_type):
    await env.space(owner=owner, space_type=space_type)
    await env.listed("g1")
    assert await env.svc.sync_snapshot("sp-1") is False
    space = await env.spaces.get("sp-1")
    assert space is not None and space.gfs_servers is None
    assert space.config_sequence == 3
    assert env.events == []


async def test_unknown_space_and_no_identity(env, db):
    assert await env.svc.sync_snapshot("nope") is False
    bare = GfsServerSelectionService(
        space_repo=env.spaces, gfs_repo=env.gfs, bus=env.bus
    )
    await env.space()
    await env.listed("g1")
    assert await bare.sync_snapshot("sp-1") is False
    assert env.events == []


async def test_leaving_the_public_tiers_signs_an_empty_list(env):
    """Even with the publication row still there (the withdrawal could not
    reach the GFS), a space that is no longer public / global tells members
    to stop using every server — triggered by the config change itself."""
    env.svc.wire()
    await env.space()
    await env.listed("g1")
    await env.svc.sync_snapshot("sp-1")
    space = await env.spaces.get("sp-1")
    assert space is not None
    await env.spaces.save(dataclasses.replace(space, space_type=SpaceType.PRIVATE))
    await env.bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type=SpaceConfigEventType.FEATURE_CHANGED.value,
            payload={},
            sequence=4,
        )
    )
    space = await env.spaces.get("sp-1")
    assert space is not None and space.gfs_servers is not None
    assert space.gfs_servers.servers() == ((), 0)
    assert verify_gfs_server_list(
        space.gfs_servers, space_id="sp-1", owner_instance_id=OWN
    )
    assert space.config_sequence == 5
    assert [e.event_type for e in env.events][-1] == (
        SpaceConfigEventType.GFS_SERVERS_CHANGED.value
    )
    # …once: the next change has nothing more to say.
    assert await env.svc.sync_snapshot("sp-1") is False


async def test_concurrent_snapshots_sign_once(env):
    await env.space()
    await env.listed("g1")
    results = await asyncio.gather(*(env.svc.sync_snapshot("sp-1") for _ in range(5)))
    assert sorted(results) == [False] * 4 + [True]
    space = await env.spaces.get("sp-1")
    assert space is not None and space.config_sequence == 4
    assert len(env.events) == 1


# ── Read side ─────────────────────────────────────────────────────────────


async def _store_owner_list(env, urls: list[str]) -> None:
    from_wire = [
        {
            "gfs_instance_id": f"gfs-{i}",
            "inbox_url": url,
            "public_key": f"{i + 1:02x}" * 32,
            "gfs_key_suite": "ed25519",
            "display_name": "",
        }
        for i, url in enumerate(urls)
    ]
    other = generate_identity_keypair()
    await env.space(owner=derive_instance_id(other.public_key))
    signed = sign_gfs_server_list(
        space_id="sp-1",
        servers=tuple(GfsServer.from_wire(e) for e in from_wire),
        issued_at=1,
        owner_seed=other.private_key,
    )
    await env.spaces.set_gfs_servers("sp-1", signed)


@pytest.mark.parametrize(
    "private_url",
    [
        "http://127.0.0.1:9000",
        "http://[::1]:9000",
        "http://10.0.0.7",
        "http://169.254.169.254",
    ],
)
async def test_member_servers_drop_private_hosts_we_do_not_hold(
    env, caplog, private_url
):
    await _store_owner_list(env, ["https://public.example", private_url])
    with caplog.at_level(logging.WARNING):
        servers = await env.svc.member_servers("sp-1")
    assert servers is not None
    assert [s.inbox_url for s in servers] == ["https://public.example"]
    assert "unusable here" in caplog.text
    # The same GFS at the same URL as one of our own connections is fine.
    await env.gfs.save(
        GfsConnection(
            id="mine",
            gfs_instance_id="gfs-1",
            display_name="mine",
            public_key="02" * 32,
            inbox_url=private_url,
            status="active",
            paired_at="2026-10-01T00:00:00+00:00",
        )
    )
    servers = await env.svc.member_servers("sp-1")
    assert servers is not None
    assert [s.inbox_url for s in servers] == ["https://public.example", private_url]


async def test_member_servers_none_when_unknown(env):
    await env.space()
    assert await env.svc.member_servers("sp-1") is None
    assert await env.svc.member_servers("nope") is None


# ── End to end ────────────────────────────────────────────────────────────


async def test_owner_snapshot_reaches_a_member_household(env, tmp_dir):
    """Carriage end to end: the owner's signed snapshot rides the real
    ``SpaceConfigOutbound`` broadcast and the member's real inbound config
    handler stores it."""
    fed = MagicMock()
    fed._own_instance_id = OWN
    fed.broadcast_to_space_members = AsyncMock()
    SpaceConfigOutbound(
        bus=env.bus, federation_service=fed, space_repo=env.spaces
    ).wire()
    await env.space()
    await env.listed("g1")
    await env.svc.sync_snapshot("sp-1")
    fed.broadcast_to_space_members.assert_awaited_once()
    payload = fed.broadcast_to_space_members.await_args.args[2]
    wire = payload["space_meta"]["gfs_servers"]
    assert [e["gfs_instance_id"] for e in wire["servers"]] == ["inst-g1"]
    assert wire["owner_pk"] == _KP.public_key.hex()
    # Only inside ``space_meta`` — never in the flat legacy part.
    assert "gfs_servers" not in payload

    member_db = AsyncDatabase(tmp_dir / "member.db", batch_timeout_ms=10)
    await member_db.startup()
    try:
        member_spaces = SqliteSpaceRepo(member_db)
        owner_row = await env.spaces.get("sp-1")
        assert owner_row is not None
        await member_spaces.save(dataclasses.replace(owner_row, config_sequence=3))
        inbound = FederationInboundService(
            bus=EventBus(),
            conversation_repo=SqliteConversationRepo(member_db),
            space_post_repo=SqliteSpacePostRepo(member_db),
            space_repo=member_spaces,
            user_repo=SqliteUserRepo(member_db),
            space_remote_member_repo=SqliteSpaceRemoteMemberRepo(member_db),
        )
        inbound._federation_service = SimpleNamespace(own_instance_id="member-i")
        await inbound._on_space_config_changed(
            FederationEvent(
                msg_id="m-1",
                event_type=FederationEventType.SPACE_CONFIG_CHANGED,
                from_instance=OWN,
                to_instance="member-i",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
                space_id="sp-1",
            )
        )
        mirrored = await member_spaces.get("sp-1")
        assert mirrored is not None
        assert mirrored.gfs_servers == owner_row.gfs_servers
        assert mirrored.config_sequence == owner_row.config_sequence
    finally:
        await member_db.shutdown()


# ── The owner's choice (set / add / remove, reconcile) ────────────────────


class _FakePublisher:
    """Stands in for :class:`GfsConnectionService`: records every call (in
    one shared ``log`` with the members' list broadcasts) and writes the
    publication row the real publish writes on success."""

    def __init__(self, env: _Env, *, fail: frozenset[str] = frozenset()) -> None:
        self._env = env
        self.fail = set(fail)
        self.calls: list[tuple[str, str, bool]] = []

    async def publish_space(self, space_id, gfs_id, *, announce=True):
        self.calls.append(("publish", gfs_id, announce))
        self._env.log.append(f"publish:{gfs_id}")
        if gfs_id in self.fail:
            raise GfsConnectionError("down")
        return await self._env.gfs.publish_space(space_id, gfs_id)

    async def unpublish_space(self, space_id, gfs_id, *, announce=True):
        self.calls.append(("unpublish", gfs_id, announce))
        self._env.log.append(f"unpublish:{gfs_id}")
        if gfs_id in self.fail:
            raise GfsConnectionError("down")
        await self._env.gfs.unpublish_space(space_id, gfs_id)


@pytest.fixture
async def owner_env(db) -> _Env:
    env = _Env(db)
    env.log = []
    env.users = SqliteUserRepo(db)
    for username, user_id in (("anna", "uid-anna"), ("ben", "uid-ben")):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, user_id, username),
        )
    env.publisher = _FakePublisher(env)
    env.svc = GfsServerSelectionService(
        space_repo=env.spaces,
        gfs_repo=env.gfs,
        bus=env.bus,
        user_repo=env.users,
        gfs_connection_service=env.publisher,
    )
    env.svc.attach_identity(own_instance_id=OWN, own_signing_key=_KP.private_key)

    async def _log_event(event: SpaceConfigChanged) -> None:
        env.log.append(f"event:{event.event_type}")

    env.bus.subscribe(SpaceConfigChanged, _log_event)
    return env


async def _owned_space(
    env: _Env, *, space_type: SpaceType = SpaceType.GLOBAL, owner: str = OWN
) -> None:
    await env.space(space_type=space_type, owner=owner)
    await env.spaces.save_member(
        SpaceMember(
            space_id="sp-1",
            user_id="uid-anna",
            role=SpaceRole.OWNER,
            joined_at="2026-10-01T00:00:00+00:00",
        )
    )
    await env.spaces.save_member(
        SpaceMember(
            space_id="sp-1",
            user_id="uid-ben",
            role=SpaceRole.ADMIN,
            joined_at="2026-10-01T00:00:00+00:00",
        )
    )


async def _connected(env: _Env, *gfs_ids: str, status: str = "active") -> None:
    for gfs_id in gfs_ids:
        await env.gfs.save(
            GfsConnection(
                id=gfs_id,
                gfs_instance_id=f"inst-{gfs_id}",
                display_name=f"GFS {gfs_id}",
                public_key=(gfs_id.encode().hex() * 32)[:64],
                inbox_url=f"https://{gfs_id}.example",
                status=status,
                paired_at="2026-10-01T00:00:00+00:00",
            )
        )


def _list_events(env: _Env) -> list[SpaceConfigChanged]:
    return [
        e
        for e in env.events
        if e.event_type == SpaceConfigEventType.GFS_SERVERS_CHANGED.value
    ]


async def test_a_public_space_never_gets_a_list(env):
    """Public is never on a GFS: even a stray publication row is not handed
    to members."""
    await env.space(space_type=SpaceType.PUBLIC)
    await env.listed("g1")
    assert await env.svc.sync_snapshot("sp-1") is False
    space = await env.spaces.get("sp-1")
    assert space is not None and space.gfs_servers is None


async def test_set_servers_diffs_and_tells_members_once(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1", "g2", "g3")
    await env.gfs.publish_space("sp-1", "g1")
    await env.gfs.publish_space("sp-1", "g3")

    outcome = await env.svc.set_servers(
        "sp-1", actor_username="anna", gfs_ids=["g1", "g2"]
    )

    assert sorted(outcome.listed) == ["g1", "g2"]
    assert outcome.failed == ()
    # g1 untouched, g2 published, g3 withdrawn — never a per-call announce.
    assert env.publisher.calls == [
        ("publish", "g2", False),
        ("unpublish", "g3", False),
    ]
    # Members are told once, after every listing change landed.
    assert env.log == [
        "publish:g2",
        "unpublish:g3",
        f"event:{SpaceConfigEventType.GFS_SERVERS_CHANGED.value}",
    ]
    assert len(_list_events(env)) == 1
    assert await env.listed_ids() == ["inst-g1", "inst-g2"]


async def test_set_servers_reports_a_failing_gfs_and_keeps_going(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1", "g2")
    env.publisher.fail = {"g1"}
    outcome = await env.svc.set_servers(
        "sp-1", actor_username="anna", gfs_ids=["g1", "g2"]
    )
    assert outcome.listed == ("g2",)
    assert outcome.failed == (("g1", "publish_failed"),)
    assert outcome.to_dict() == {
        "listed": ["g2"],
        "failed": [{"gfs_id": "g1", "reason": "publish_failed"}],
    }
    # Only the server that took the listing is in the members' list.
    assert await env.listed_ids() == ["inst-g2"]


async def test_set_servers_reports_a_failed_withdrawal(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1")
    await env.gfs.publish_space("sp-1", "g1")
    env.publisher.fail = {"g1"}
    outcome = await env.svc.set_servers("sp-1", actor_username="anna", gfs_ids=[])
    assert outcome.listed == ("g1",)
    assert outcome.failed == (("g1", "unpublish_failed"),)


@pytest.mark.security
@pytest.mark.parametrize("actor", ["ben", "nobody"])
async def test_only_the_space_owner_changes_the_servers(owner_env, actor):
    """An admin (or anyone else) can't aim the space at a server."""
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1")
    with pytest.raises(SpacePermissionError):
        await env.svc.set_servers("sp-1", actor_username=actor, gfs_ids=["g1"])
    with pytest.raises(SpacePermissionError):
        await env.svc.add_server("sp-1", actor_username=actor, gfs_id="g1")
    with pytest.raises(SpacePermissionError):
        await env.svc.remove_server("sp-1", actor_username=actor, gfs_id="g1")
    assert env.publisher.calls == []
    assert env.events == []


@pytest.mark.security
async def test_a_space_owned_elsewhere_is_never_listed_from_here(owner_env):
    env = owner_env
    await _owned_space(env, owner="other-household")
    await _connected(env, "g1")
    with pytest.raises(SpacePermissionError):
        await env.svc.set_servers("sp-1", actor_username="anna", gfs_ids=["g1"])
    with pytest.raises(SpacePermissionError):
        await env.svc.remove_server(
            "sp-1", actor_username="anna", gfs_id="g1", household_admin=True
        )
    assert env.publisher.calls == []


async def test_an_unknown_space_is_not_found(owner_env):
    with pytest.raises(KeyError):
        await owner_env.svc.set_servers("nope", actor_username="anna", gfs_ids=[])


@pytest.mark.security
async def test_servers_for_a_public_space_need_a_tier_change(owner_env):
    env = owner_env
    await _owned_space(env, space_type=SpaceType.PUBLIC)
    await _connected(env, "g1")
    with pytest.raises(TierChangeRequiredError):
        await env.svc.set_servers("sp-1", actor_username="anna", gfs_ids=["g1"])
    with pytest.raises(TierChangeRequiredError):
        await env.svc.add_server("sp-1", actor_username="anna", gfs_id="g1")
    assert env.publisher.calls == []


async def test_servers_for_a_private_space_are_refused(owner_env):
    env = owner_env
    await _owned_space(env, space_type=SpaceType.PRIVATE)
    await _connected(env, "g1")
    with pytest.raises(SpaceNotPublishableError) as exc_info:
        await env.svc.add_server("sp-1", actor_username="anna", gfs_id="g1")
    assert exc_info.value.code == "SPACE_NOT_PUBLIC"


async def test_an_empty_set_only_withdraws_whatever_the_tier(owner_env):
    """A space that left the global tier may still be withdrawn."""
    env = owner_env
    await _owned_space(env, space_type=SpaceType.PUBLIC)
    await _connected(env, "g1")
    await env.gfs.publish_space("sp-1", "g1")
    outcome = await env.svc.set_servers("sp-1", actor_username="anna", gfs_ids=[])
    assert outcome.listed == ()
    assert env.publisher.calls == [("unpublish", "g1", False)]


@pytest.mark.parametrize(
    ("gfs_ids", "reason"),
    [
        (None, "malformed"),
        ("g1", "malformed"),
        (["g1", 3], "malformed"),
        ([""], "malformed"),
        (["g-unknown"], "unknown"),
        (["g-pending"], "unknown"),
        ([f"g{i}" for i in range(MAX_GFS_SERVERS + 1)], "too_many"),
    ],
)
async def test_a_bad_choice_is_refused(owner_env, gfs_ids, reason):
    env = owner_env
    await _owned_space(env)
    await _connected(env, *[f"g{i}" for i in range(MAX_GFS_SERVERS + 1)])
    await _connected(env, "g-pending", status="pending")
    with pytest.raises(GfsSelectionInvalidError) as exc_info:
        await env.svc.set_servers("sp-1", actor_username="anna", gfs_ids=gfs_ids)
    assert exc_info.value.params["reason"] == reason
    assert env.publisher.calls == []


async def test_check_selection_dedupes_and_requires_when_asked(owner_env):
    env = owner_env
    assert await env.svc.check_selection(None, required=True) == ()
    await _connected(env, "g1", "g2")
    assert await env.svc.check_selection(["g2", "g1", "g2"]) == ("g2", "g1")
    assert await env.svc.check_selection(None) == ()
    with pytest.raises(GfsSelectionRequiredError):
        await env.svc.check_selection([], required=True)


async def test_still_active_drops_disconnected_servers(owner_env):
    env = owner_env
    await _connected(env, "g1")
    await _connected(env, "g2", status="suspended")
    assert await env.svc.still_active(("g1", "g2", "g9", "g1")) == ("g1",)


async def test_add_server_lists_and_tells_members(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1")
    pub = await env.svc.add_server("sp-1", actor_username="anna", gfs_id="g1")
    assert pub.gfs_connection_id == "g1"
    assert env.publisher.calls == [("publish", "g1", False)]
    assert env.log[-1] == f"event:{SpaceConfigEventType.GFS_SERVERS_CHANGED.value}"
    assert await env.listed_ids() == ["inst-g1"]


async def test_add_server_refuses_an_inactive_connection(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1", status="pending")
    with pytest.raises(GfsConnectionError):
        await env.svc.add_server("sp-1", actor_username="anna", gfs_id="g1")
    with pytest.raises(GfsConnectionError):
        await env.svc.add_server("sp-1", actor_username="anna", gfs_id="nope")
    assert env.publisher.calls == []


async def test_add_server_failure_lists_nothing(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1")
    env.publisher.fail = {"g1"}
    with pytest.raises(GfsConnectionError):
        await env.svc.add_server("sp-1", actor_username="anna", gfs_id="g1")
    assert await env.gfs.list_publications_for_space("sp-1") == []


async def test_add_server_holds_the_cap(owner_env):
    env = owner_env
    await _owned_space(env)
    ids = [f"g{i}" for i in range(MAX_GFS_SERVERS + 1)]
    await _connected(env, *ids)
    for gfs_id in ids[:MAX_GFS_SERVERS]:
        await env.gfs.publish_space("sp-1", gfs_id)
    with pytest.raises(GfsSelectionInvalidError) as exc_info:
        await env.svc.add_server("sp-1", actor_username="anna", gfs_id=ids[-1])
    assert exc_info.value.params == {"reason": "too_many", "max": MAX_GFS_SERVERS}
    # Refreshing one it already has is fine at the cap.
    await env.svc.add_server("sp-1", actor_username="anna", gfs_id=ids[0])


async def test_remove_server_by_owner_or_household_admin(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1", "g2")
    await env.gfs.publish_space("sp-1", "g1")
    await env.gfs.publish_space("sp-1", "g2")
    await env.svc.remove_server("sp-1", actor_username="anna", gfs_id="g1")
    # A household admin (no role in the space) may withdraw too.
    await env.svc.remove_server(
        "sp-1", actor_username="nobody", gfs_id="g2", household_admin=True
    )
    assert env.publisher.calls == [
        ("unpublish", "g1", False),
        ("unpublish", "g2", False),
    ]
    assert await env.gfs.list_publications_for_space("sp-1") == []


async def test_apply_without_a_publisher_is_a_wiring_error(env):
    with pytest.raises(RuntimeError):
        await env.svc.apply("sp-1", ("g1",))


async def test_describe(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1")
    space = await env.spaces.get("sp-1")
    # Owned, nothing signed yet: the listings are the truth.
    assert await env.svc.describe(space) == ([], True)
    await env.gfs.publish_space("sp-1", "g1")
    assert await env.svc.describe(space) == (
        [
            {
                "display_name": "GFS g1",
                "inbox_url": "https://g1.example",
                "connected": True,
            }
        ],
        False,
    )
    await env.svc.sync_snapshot("sp-1")
    space = await env.spaces.get("sp-1")
    servers, unlisted = await env.svc.describe(space)
    assert servers == [
        {"display_name": "GFS g1", "inbox_url": "https://g1.example", "connected": True}
    ]
    assert unlisted is False
    # Not a connection of ours (a member's view): connected=False.
    await env.gfs.delete("g1")
    servers, _ = await env.svc.describe(space)
    assert servers == [
        {
            "display_name": "GFS g1",
            "inbox_url": "https://g1.example",
            "connected": False,
        }
    ]
    # Not global → nothing to show.
    public = dataclasses.replace(space, space_type=SpaceType.PUBLIC)
    assert await env.svc.describe(public) == (None, False)
    # A member's copy with no list known yet.
    remote = dataclasses.replace(space, owner_instance_id="elsewhere", gfs_servers=None)
    assert await env.svc.describe(remote) == (None, False)


async def test_describe_flags_global_not_listed(owner_env):
    env = owner_env
    await _owned_space(env)
    await env.svc.sync_snapshot("sp-1")  # signs the empty list
    space = await env.spaces.get("sp-1")
    assert space.gfs_servers is not None
    assert await env.svc.describe(space) == ([], True)


async def test_owner_view(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1", "g2")
    await env.gfs.publish_space("sp-1", "g1", "pending")
    view = await env.svc.owner_view("sp-1", actor_username="anna")
    assert view["space_type"] == "global"
    assert view["gfs_ids"] == ["g1"]
    assert view["max"] == MAX_GFS_SERVERS
    assert {
        a["gfs_id"]: (a["listed"], a["listing_status"]) for a in view["available"]
    } == {
        "g1": (True, "pending"),
        "g2": (False, None),
    }
    with pytest.raises(SpacePermissionError):
        await env.svc.owner_view("sp-1", actor_username="ben")


async def test_reconcile_turns_a_listed_public_space_global(owner_env):
    """Hand-published public spaces become global (their listings are kept,
    no quorum); the members hear the tier and the list."""
    env = owner_env
    await _owned_space(env, space_type=SpaceType.PUBLIC)
    await _connected(env, "g1")
    await env.gfs.publish_space("sp-1", "g1")

    assert await env.svc.reconcile_on_startup() == 1

    space = await env.spaces.get("sp-1")
    assert space is not None and space.space_type is SpaceType.GLOBAL
    assert await env.listed_ids() == ["inst-g1"]
    assert [e.event_type for e in env.events] == [
        SpaceConfigEventType.PUBLIC_MODE_CHANGED.value,
        SpaceConfigEventType.GFS_SERVERS_CHANGED.value,
    ]
    assert env.events[0].payload == {"space_type": "global"}
    assert await env.spaces.get_config_author("sp-1") == OWN
    # Nothing was sent to any GFS: the listing already exists.
    assert env.publisher.calls == []


async def test_reconcile_signs_a_list_for_a_global_space_without_one(owner_env):
    env = owner_env
    await _owned_space(env)
    await _connected(env, "g1")
    await env.gfs.publish_space("sp-1", "g1")
    assert await env.svc.reconcile_on_startup() == 1
    assert await env.listed_ids() == ["inst-g1"]
    assert env.publisher.calls == []


async def test_reconcile_is_idempotent_and_leaves_others_alone(owner_env):
    env = owner_env
    await _owned_space(env, space_type=SpaceType.PUBLIC)
    await _connected(env, "g1")
    await env.gfs.publish_space("sp-1", "g1")
    # An unlisted public space and a space owned elsewhere stay as they are.
    await env.space("sp-plain", space_type=SpaceType.PUBLIC)
    await env.space("sp-remote", space_type=SpaceType.PUBLIC, owner="elsewhere")
    await env.gfs.publish_space("sp-remote", "g1")
    await env.space("sp-remote-g", space_type=SpaceType.GLOBAL, owner="elsewhere")

    assert await env.svc.reconcile_on_startup() == 1
    events = len(env.events)
    assert await env.svc.reconcile_on_startup() == 0
    assert len(env.events) == events

    assert (await env.spaces.get("sp-plain")).space_type is SpaceType.PUBLIC
    remote = await env.spaces.get("sp-remote")
    assert remote.space_type is SpaceType.PUBLIC and remote.gfs_servers is None
    assert (await env.spaces.get("sp-remote-g")).gfs_servers is None


async def test_reconcile_without_identity_does_nothing(owner_env):
    env = owner_env
    bare = GfsServerSelectionService(
        space_repo=env.spaces, gfs_repo=env.gfs, bus=env.bus
    )
    await _owned_space(env, space_type=SpaceType.PUBLIC)
    await _connected(env, "g1")
    await env.gfs.publish_space("sp-1", "g1")
    assert await bare.reconcile_on_startup() == 0
    assert (await env.spaces.get("sp-1")).space_type is SpaceType.PUBLIC
