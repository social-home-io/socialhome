"""Verify SpaceService.add_member enforces the §CP.F1 age gate."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import SpacePermissionError
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.cp_repo import SqliteCpRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.child_protection_service import (
    ChildProtectionService,
)
from socialhome.services.space_service import SpaceService


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('admin', 'admin-id', 'Admin', 1)",
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('lila', 'lila-id', 'Lila', 0)",
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-adult', 'X', ?, 'admin', ?)",
        (iid, "ab" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role)"
        " VALUES('sp-adult', 'admin-id', 'owner')",
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db)
    space_post_repo = SqliteSpacePostRepo(db)
    space_svc = SpaceService(
        space_repo,
        space_post_repo,
        user_repo,
        bus,
        own_instance_id=iid,
    )
    cp_svc = ChildProtectionService(SqliteCpRepo(db), user_repo, bus)
    space_svc.attach_child_protection(cp_svc)
    yield space_svc, cp_svc
    await db.shutdown()


# ─── §CP.F1 enforcement ──────────────────────────────────────────────────


async def test_add_member_blocks_underage_minor(env):
    space_svc, cp_svc = env
    await cp_svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await cp_svc.update_space_age_gate(
        "sp-adult",
        min_age=18,
        target_audience="adult",
        actor_user_id="admin-id",
    )
    with pytest.raises(SpacePermissionError, match="18"):
        await space_svc.add_member(
            "sp-adult",
            actor_username="admin",
            user_id="lila-id",
        )


async def test_add_member_allows_minor_above_min_age(env):
    space_svc, cp_svc = env
    await cp_svc.enable_protection(
        minor_username="lila",
        declared_age=15,
        actor_user_id="admin-id",
    )
    await cp_svc.update_space_age_gate(
        "sp-adult",
        min_age=13,
        target_audience="teen",
        actor_user_id="admin-id",
    )
    member = await space_svc.add_member(
        "sp-adult",
        actor_username="admin",
        user_id="lila-id",
    )
    assert member.user_id == "lila-id"


async def test_add_member_allows_unprotected_user(env):
    space_svc, cp_svc = env
    await cp_svc.update_space_age_gate(
        "sp-adult",
        min_age=18,
        target_audience="adult",
        actor_user_id="admin-id",
    )
    # lila has no CP enabled → no age gate enforcement.
    member = await space_svc.add_member(
        "sp-adult",
        actor_username="admin",
        user_id="lila-id",
    )
    assert member.user_id == "lila-id"


async def test_add_member_no_cp_attached_works_unchanged(env):
    space_svc, cp_svc = env
    space_svc.attach_child_protection(None)
    await cp_svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await cp_svc.update_space_age_gate(
        "sp-adult",
        min_age=18,
        target_audience="adult",
        actor_user_id="admin-id",
    )
    # Detached CP → no enforcement.
    member = await space_svc.add_member(
        "sp-adult",
        actor_username="admin",
        user_id="lila-id",
    )
    assert member.user_id == "lila-id"
