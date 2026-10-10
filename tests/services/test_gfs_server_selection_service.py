"""Tests for :class:`GfsServerSelectionService` — the owner's
``spaces.gfs_servers`` snapshot (migration 0089), derived from its active
GFS listings and handed to members in a sequence-raised config change."""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpaceConfigChanged
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    GfsConnection,
)
from socialhome.domain.gfs_server import MAX_GFS_SERVERS
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceConfigEventType,
    SpaceFeatures,
    SpaceType,
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

OWN = "own-household"


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
            space_repo=self.spaces,
            gfs_repo=self.gfs,
            bus=self.bus,
            own_instance_id=OWN,
        )

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
    ) -> None:
        await self.gfs.save(
            GfsConnection(
                id=gfs_id,
                gfs_instance_id=f"inst-{gfs_id}",
                display_name=f"GFS {gfs_id}",
                public_key="AB" * 32,
                inbox_url=inbox_url or f"https://{gfs_id}.example",
                status=conn_status,
                paired_at="2026-10-01T00:00:00+00:00",
            )
        )
        await self.gfs.publish_space(space_id, gfs_id, status)


@pytest.fixture
def env(db) -> _Env:
    return _Env(db)


async def test_snapshot_records_the_active_listings_and_tells_members(env):
    await env.space()
    await env.listed("g2")
    await env.listed("g1")
    assert await env.svc.sync_snapshot("sp-1") is True

    space = await env.spaces.get("sp-1")
    assert space is not None
    assert [s.gfs_instance_id for s in space.gfs_servers] == ["inst-g1", "inst-g2"]
    first = space.gfs_servers[0]
    assert first.inbox_url == "https://g1.example"
    assert first.public_key == "ab" * 32
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
    assert await env.svc.sync_snapshot("sp-1") is False
    space = await env.spaces.get("sp-1")
    assert space is not None and space.config_sequence == 4
    assert len(env.events) == 1


async def test_pending_listing_and_suspended_connection_are_left_out(env):
    await env.space()
    await env.listed("g1")
    await env.listed("g2", status="pending")
    await env.listed("g3", conn_status="suspended")
    await env.svc.sync_snapshot("sp-1")
    space = await env.spaces.get("sp-1")
    assert space is not None
    assert [s.gfs_instance_id for s in space.gfs_servers] == ["inst-g1"]


async def test_listed_nowhere_is_an_empty_list_not_unknown(env):
    await env.space()
    await env.listed("g1")
    await env.svc.sync_snapshot("sp-1")
    await env.gfs.unpublish_space("sp-1", "g1")
    assert await env.svc.sync_snapshot("sp-1") is True
    space = await env.spaces.get("sp-1")
    assert space is not None and space.gfs_servers == ()
    assert len(env.events) == 2


async def test_a_connection_with_an_unusable_url_is_dropped_and_logged(env, caplog):
    await env.space()
    await env.listed("g1")
    await env.listed("g2", inbox_url="http://public.example")
    with caplog.at_level(logging.WARNING):
        await env.svc.sync_snapshot("sp-1")
    space = await env.spaces.get("sp-1")
    assert space is not None
    assert [s.gfs_instance_id for s in space.gfs_servers] == ["inst-g1"]
    assert "g2" in caplog.text


async def test_list_is_capped(env):
    await env.space()
    for i in range(MAX_GFS_SERVERS + 2):
        await env.listed(f"g{i:02d}")
    await env.svc.sync_snapshot("sp-1")
    space = await env.spaces.get("sp-1")
    assert space is not None and len(space.gfs_servers) == MAX_GFS_SERVERS


@pytest.mark.parametrize(
    "owner, space_type",
    [
        ("someone-else", SpaceType.GLOBAL),
        (OWN, SpaceType.PRIVATE),
        (OWN, SpaceType.HOUSEHOLD),
    ],
)
async def test_only_listed_tiers_of_spaces_we_own(env, owner, space_type):
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


async def test_unknown_space(env):
    assert await env.svc.sync_snapshot("nope") is False
    assert env.events == []


async def test_owner_snapshot_reaches_a_member_household(env, tmp_dir):
    """Carriage end to end: the owner's snapshot rides the real
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
    assert [e["gfs_instance_id"] for e in payload["space_meta"]["gfs_servers"]] == [
        "inst-g1"
    ]
    # Only inside ``space_meta`` — never in the flat legacy part.
    assert "gfs_servers" not in payload

    member_db = AsyncDatabase(tmp_dir / "member.db", batch_timeout_ms=10)
    await member_db.startup()
    try:
        member_spaces = SqliteSpaceRepo(member_db)
        owner_row = await env.spaces.get("sp-1")
        assert owner_row is not None
        await member_spaces.save(
            dataclasses.replace(owner_row, config_sequence=3, gfs_servers=None)
        )
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
