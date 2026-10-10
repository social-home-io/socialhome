"""Tests for :class:`GfsServerSelectionService` — the owner's signed
``spaces.gfs_servers`` snapshot (migration 0091), derived from its active
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
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceConfigEventType,
    SpaceFeatures,
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
    """A list signed after a clock step back still outranks the stored one."""
    await env.space()
    far_future = sign_gfs_server_list(
        space_id="sp-1", servers=(), issued_at=2**60, owner_seed=_KP.private_key
    )
    await env.spaces.set_gfs_servers("sp-1", far_future)
    await env.listed("g1")
    await env.svc.sync_snapshot("sp-1")
    space = await env.spaces.get("sp-1")
    assert space is not None and space.gfs_servers is not None
    assert space.gfs_servers.issued_at == 2**60 + 1


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


async def test_public_space_is_snapshotted_too(env):
    await env.space(space_type=SpaceType.PUBLIC)
    await env.listed("g1")
    assert await env.svc.sync_snapshot("sp-1") is True


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
