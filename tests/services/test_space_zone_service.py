"""Tests for ``socialhome.services.space_zone_service`` (§23.8.7).

The service layer is admin-gated CRUD over the per-space zone catalogue.
We exercise:

* Happy path create / update / delete and the corresponding domain
  events on the bus.
* Admin gate — non-admin members get :class:`SpacePermissionError` on
  any write; reads are allowed for any member.
* Validation — radius bounds, color hex shape, name non-empty + unique.
* 50-zones-per-space cap.
* GPS truncation to 4 dp on persisted lat/lon.

Tests follow the codebase convention: plain ``async def test_xxx()``
functions, in-memory SQLite via the shared ``tmp_dir`` fixture, no real
network. Helpers live alongside the tests so each scenario is
self-contained.
"""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpaceZoneDeleted, SpaceZoneUpserted
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpacePermissionError,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.space_zone_repo import SqliteSpaceZoneRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_zone_service import (
    MAX_RADIUS_M,
    MAX_ZONES_PER_SPACE,
    MIN_RADIUS_M,
    SpaceZoneLimitError,
    SpaceZoneNameConflictError,
    SpaceZoneNotFoundError,
    SpaceZoneService,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    admin_uid = derive_user_id(kp.public_key, "admin")
    member_uid = derive_user_id(kp.public_key, "alice")
    outsider_uid = derive_user_id(kp.public_key, "stranger")
    for username, uid, name in (
        ("admin", admin_uid, "Admin"),
        ("alice", member_uid, "Alice"),
        ("stranger", outsider_uid, "Stranger"),
    ):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, uid, name),
        )

    space_repo = SqliteSpaceRepo(db)
    space = Space(
        id="sp_test",
        name="Test Space",
        owner_instance_id=iid,
        owner_username="admin",
        identity_public_key=kp.public_key.hex(),
        config_sequence=1,
        features=SpaceFeatures(location=True),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    await space_repo.save(space)
    await space_repo.save_member(
        SpaceMember(
            space_id="sp_test",
            user_id=admin_uid,
            role="owner",
            joined_at="2026-04-27T00:00:00+00:00",
        ),
    )
    await space_repo.save_member(
        SpaceMember(
            space_id="sp_test",
            user_id=member_uid,
            role="member",
            joined_at="2026-04-27T00:00:00+00:00",
        ),
    )
    # `stranger` exists as a user but is NOT a member of sp_test.

    bus = EventBus()
    captured: list[object] = []

    async def _capture(ev):
        captured.append(ev)

    bus.subscribe(SpaceZoneUpserted, _capture)
    bus.subscribe(SpaceZoneDeleted, _capture)

    svc = SpaceZoneService(
        SqliteSpaceZoneRepo(db),
        space_repo,
        SqliteUserRepo(db),
        bus,
    )

    class E:
        pass

    e = E()
    e.db = db
    e.svc = svc
    e.bus = bus
    e.events = captured
    e.admin_uid = admin_uid
    e.member_uid = member_uid
    e.outsider_uid = outsider_uid
    yield e
    await db.shutdown()


# ─── Create ──────────────────────────────────────────────────────────────


async def test_create_zone_persists_truncates_and_publishes(env):
    zone = await env.svc.create_zone(
        "sp_test",
        "admin",
        name="The Workshop",
        latitude=47.376912345,
        longitude=8.541798765,
        radius_m=150,
        color="#3B82F6",
    )
    assert zone.space_id == "sp_test"
    assert zone.name == "The Workshop"
    assert zone.latitude == 47.3769
    assert zone.longitude == 8.5418
    assert zone.radius_m == 150
    assert zone.color == "#3b82f6"  # normalised lowercase
    assert zone.id.startswith("z_")
    assert zone.created_by == env.admin_uid

    persisted = await env.svc.list_zones("sp_test", env.admin_uid)
    assert [z.id for z in persisted] == [zone.id]
    assert persisted[0].latitude == 47.3769

    [event] = env.events
    assert isinstance(event, SpaceZoneUpserted)
    assert event.zone_id == zone.id
    assert event.latitude == 47.3769


async def test_create_zone_non_admin_member_rejected(env):
    with pytest.raises(SpacePermissionError):
        await env.svc.create_zone(
            "sp_test",
            "alice",  # role=member, not admin
            name="My Zone",
            latitude=47.0,
            longitude=8.0,
            radius_m=200,
        )


async def test_create_zone_outsider_rejected(env):
    with pytest.raises(SpacePermissionError):
        await env.svc.create_zone(
            "sp_test",
            "stranger",
            name="Imposter Zone",
            latitude=47.0,
            longitude=8.0,
            radius_m=200,
        )


async def test_create_zone_radius_too_small_rejected(env):
    with pytest.raises(ValueError, match="radius_m"):
        await env.svc.create_zone(
            "sp_test",
            "admin",
            name="Tiny",
            latitude=0.0,
            longitude=0.0,
            radius_m=MIN_RADIUS_M - 1,
        )


async def test_create_zone_radius_too_large_rejected(env):
    with pytest.raises(ValueError, match="radius_m"):
        await env.svc.create_zone(
            "sp_test",
            "admin",
            name="Huge",
            latitude=0.0,
            longitude=0.0,
            radius_m=MAX_RADIUS_M + 1,
        )


async def test_create_zone_bad_color_rejected(env):
    with pytest.raises(ValueError, match="color"):
        await env.svc.create_zone(
            "sp_test",
            "admin",
            name="Pink",
            latitude=0.0,
            longitude=0.0,
            radius_m=200,
            color="pink",
        )


async def test_create_zone_empty_name_rejected(env):
    with pytest.raises(ValueError, match="name"):
        await env.svc.create_zone(
            "sp_test",
            "admin",
            name="   ",
            latitude=0.0,
            longitude=0.0,
            radius_m=200,
        )


async def test_create_zone_duplicate_name_rejected(env):
    await env.svc.create_zone(
        "sp_test",
        "admin",
        name="Office",
        latitude=0.0,
        longitude=0.0,
        radius_m=200,
    )
    with pytest.raises(SpaceZoneNameConflictError):
        await env.svc.create_zone(
            "sp_test",
            "admin",
            name="Office",
            latitude=1.0,
            longitude=1.0,
            radius_m=300,
        )


async def test_create_zone_50_cap_enforced(env):
    for i in range(MAX_ZONES_PER_SPACE):
        await env.svc.create_zone(
            "sp_test",
            "admin",
            name=f"Zone {i}",
            latitude=float(i % 90),
            longitude=float(i % 180),
            radius_m=200,
        )
    with pytest.raises(SpaceZoneLimitError):
        await env.svc.create_zone(
            "sp_test",
            "admin",
            name="One Too Many",
            latitude=1.0,
            longitude=1.0,
            radius_m=200,
        )


# ─── Read ────────────────────────────────────────────────────────────────


async def test_list_zones_member_allowed(env):
    await env.svc.create_zone(
        "sp_test",
        "admin",
        name="Office",
        latitude=0.0,
        longitude=0.0,
        radius_m=200,
    )
    zones = await env.svc.list_zones("sp_test", env.member_uid)
    assert [z.name for z in zones] == ["Office"]


async def test_list_zones_outsider_rejected(env):
    with pytest.raises(SpacePermissionError):
        await env.svc.list_zones("sp_test", env.outsider_uid)


# ─── Update ──────────────────────────────────────────────────────────────


async def test_update_zone_partial_change(env):
    z = await env.svc.create_zone(
        "sp_test",
        "admin",
        name="Office",
        latitude=47.0,
        longitude=8.0,
        radius_m=200,
        color="#aabbcc",
    )
    env.events.clear()
    updated = await env.svc.update_zone(
        "sp_test",
        z.id,
        "admin",
        radius_m=400,
    )
    assert updated.radius_m == 400
    assert updated.name == "Office"
    assert updated.color == "#aabbcc"  # untouched
    assert isinstance(env.events[-1], SpaceZoneUpserted)


async def test_update_zone_clear_color(env):
    z = await env.svc.create_zone(
        "sp_test",
        "admin",
        name="Office",
        latitude=47.0,
        longitude=8.0,
        radius_m=200,
        color="#aabbcc",
    )
    updated = await env.svc.update_zone(
        "sp_test",
        z.id,
        "admin",
        color=None,  # explicit None means clear
    )
    assert updated.color is None


async def test_update_zone_name_collision_rejected(env):
    await env.svc.create_zone(
        "sp_test",
        "admin",
        name="Office",
        latitude=0.0,
        longitude=0.0,
        radius_m=200,
    )
    cafe = await env.svc.create_zone(
        "sp_test",
        "admin",
        name="Cafe",
        latitude=0.1,
        longitude=0.1,
        radius_m=200,
    )
    with pytest.raises(SpaceZoneNameConflictError):
        await env.svc.update_zone(
            "sp_test",
            cafe.id,
            "admin",
            name="Office",
        )


async def test_update_zone_unknown_id_404(env):
    with pytest.raises(SpaceZoneNotFoundError):
        await env.svc.update_zone(
            "sp_test",
            "z_nonexistent",
            "admin",
            radius_m=400,
        )


async def test_update_zone_non_admin_rejected(env):
    z = await env.svc.create_zone(
        "sp_test",
        "admin",
        name="Office",
        latitude=0.0,
        longitude=0.0,
        radius_m=200,
    )
    with pytest.raises(SpacePermissionError):
        await env.svc.update_zone(
            "sp_test",
            z.id,
            "alice",
            radius_m=400,
        )


# ─── Delete ──────────────────────────────────────────────────────────────


async def test_delete_zone_publishes_event(env):
    z = await env.svc.create_zone(
        "sp_test",
        "admin",
        name="Office",
        latitude=0.0,
        longitude=0.0,
        radius_m=200,
    )
    env.events.clear()
    await env.svc.delete_zone("sp_test", z.id, "admin")
    assert await env.svc.list_zones("sp_test", env.admin_uid) == []
    [event] = env.events
    assert isinstance(event, SpaceZoneDeleted)
    assert event.zone_id == z.id
    assert event.deleted_by == env.admin_uid


async def test_delete_zone_unknown_id_404(env):
    with pytest.raises(SpaceZoneNotFoundError):
        await env.svc.delete_zone("sp_test", "z_nope", "admin")


async def test_delete_zone_non_admin_rejected(env):
    z = await env.svc.create_zone(
        "sp_test",
        "admin",
        name="Office",
        latitude=0.0,
        longitude=0.0,
        radius_m=200,
    )
    with pytest.raises(SpacePermissionError):
        await env.svc.delete_zone("sp_test", z.id, "alice")
