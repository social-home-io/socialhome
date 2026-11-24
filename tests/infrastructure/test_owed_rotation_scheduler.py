"""Tests for OwedRotationScheduler — the daily sweep paying owed content-key
rotations of followers' leaves (v_96)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import JoinMode, SpaceFeatures, SpaceRole, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.infrastructure.owed_rotation_scheduler import OwedRotationScheduler
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService

PEER = "peer-house"


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "owed.db", batch_timeout_ms=10)
    await db.startup()
    bus = EventBus()
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x09" * 32))
    svc = SpaceService(spaces, SqliteSpacePostRepo(db), users, bus, own_instance_id=iid)
    remote = SqliteSpaceRemoteMemberRepo(db)
    svc.attach_federation(
        federation_service=AsyncMock(),
        federation_repo=AsyncMock(),
        remote_member_repo=remote,
    )
    await UserService(users, bus, own_instance_public_key=kp.public_key).provision(
        username="owner", display_name="owner"
    )
    yield db, svc, remote
    await db.shutdown()


async def _owing(env, *, hours_ago: float, owner_instance_id: str | None = None):
    """A space whose follower left ``hours_ago``, after its current key."""
    db, svc, remote = env
    space = await svc.create_space(
        owner_username="owner",
        name="S",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
        features=SpaceFeatures(allow_subscribers=True),
    )
    await db.enqueue(
        "INSERT INTO space_keys(space_id, epoch, content_key_hex, created_at)"
        " VALUES(?, 0, ?, '2026-01-01T00:00:00+00:00')",
        (space.id, "aa" * 32),
    )
    await remote.add(
        space_id=space.id,
        instance_id=PEER,
        user_id="u-fan",
        user_pk=None,
        display_name="fan",
        role=SpaceRole.SUBSCRIBER.value,
    )
    await remote.remove(space.id, PEER, "u-fan")
    left = datetime.now(UTC) - timedelta(hours=hours_ago)
    await db.enqueue(
        "UPDATE space_remote_members SET removed_at=? WHERE space_id=?",
        (left.strftime("%Y-%m-%d %H:%M:%S"), space.id),
    )
    if owner_instance_id is not None:
        await db.enqueue(
            "UPDATE spaces SET owner_instance_id=? WHERE id=?",
            (owner_instance_id, space.id),
        )
    return space.id


def _minting_rotation(db):
    """Stands in for the real rotation: writes the new epoch's key row,
    which is what pays the debt."""

    async def _rotate(space_id):
        await db.enqueue(
            "INSERT INTO space_keys(space_id, epoch, content_key_hex, created_at)"
            " VALUES(?, 1, ?, ?)",
            (space_id, "bb" * 32, datetime.now(UTC).isoformat()),
        )

    return AsyncMock(side_effect=_rotate)


async def test_a_tick_pays_an_old_debt_once(env):
    db, svc, _remote = env
    space_id = await _owing(env, hours_ago=25)
    rotate = _minting_rotation(db)
    sched = OwedRotationScheduler(svc)
    with patch.object(SpaceService, "_rotate_and_distribute_space_key", rotate):
        assert await sched.tick() == 1
        # The new key paid it: a second tick rotates nothing.
        assert await sched.tick() == 0
    rotate.assert_awaited_once_with(space_id)


async def test_a_young_debt_waits_for_its_day(env):
    db, svc, _remote = env
    await _owing(env, hours_ago=3)
    rotate = _minting_rotation(db)
    with patch.object(SpaceService, "_rotate_and_distribute_space_key", rotate):
        assert await OwedRotationScheduler(svc).tick() == 0
    rotate.assert_not_awaited()


async def test_a_space_we_do_not_host_is_left_to_its_host(env):
    db, svc, _remote = env
    await _owing(env, hours_ago=25, owner_instance_id="someone-else")
    rotate = _minting_rotation(db)
    with patch.object(SpaceService, "_rotate_and_distribute_space_key", rotate):
        assert await OwedRotationScheduler(svc).tick() == 0
    rotate.assert_not_awaited()


async def test_one_failing_space_does_not_stop_the_sweep(caplog):
    now = datetime.now(UTC)
    payer = AsyncMock()
    payer.list_spaces_owing_rotation = AsyncMock(
        return_value=[
            ("sp-a", now - timedelta(days=2)),
            ("sp-b", now - timedelta(days=2)),
        ]
    )
    payer.pay_owed_rotation = AsyncMock(side_effect=[RuntimeError("boom"), True])
    with caplog.at_level("WARNING"):
        assert await OwedRotationScheduler(payer).tick() == 1
    assert [c.args[0] for c in payer.pay_owed_rotation.await_args_list] == [
        "sp-a",
        "sp-b",
    ]
    assert payer.pay_owed_rotation.await_args.kwargs == {"min_age": timedelta(hours=24)}
    assert "sp-a" in caplog.text


async def test_start_and_stop_are_idempotent():
    payer = AsyncMock()
    payer.list_spaces_owing_rotation = AsyncMock(return_value=[])
    sched = OwedRotationScheduler(payer, interval_seconds=3600.0)
    await sched.start()
    task = sched._task
    await sched.start()
    assert sched._task is task
    await asyncio.sleep(0)
    await sched.stop()
    assert sched._task is None
    await sched.stop()
    # The first tick ran at start; the loop then waited on the stop event.
    payer.list_spaces_owing_rotation.assert_awaited_once()


async def test_a_failing_listing_keeps_the_loop_alive(caplog):
    payer = AsyncMock()
    payer.list_spaces_owing_rotation = AsyncMock(side_effect=RuntimeError("db"))
    sched = OwedRotationScheduler(payer, interval_seconds=0.01)
    with caplog.at_level("WARNING"):
        await sched.start()
        await asyncio.sleep(0.05)
        await sched.stop()
    assert payer.list_spaces_owing_rotation.await_count >= 2
    assert "owed-rotation sweep failed" in caplog.text
