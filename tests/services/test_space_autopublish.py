"""§D1 auto-publish hook: flipping space_type to/from 'global' fans
publish / unpublish calls out to every active GFS connection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from social_home.crypto import derive_instance_id, generate_identity_keypair
from social_home.db.database import AsyncDatabase
from social_home.domain.space import JoinMode, SpaceType
from social_home.infrastructure.event_bus import EventBus
from social_home.repositories.space_post_repo import SqliteSpacePostRepo
from social_home.repositories.space_repo import SqliteSpaceRepo
from social_home.repositories.user_repo import SqliteUserRepo
from social_home.services.space_service import SpaceService
from social_home.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "autopublish.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            """INSERT INTO instance_identity(
                   instance_id, identity_private_key,
                   identity_public_key, routing_secret
               ) VALUES(?,?,?,?)""",
            (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
        )
        bus = EventBus()
        user_repo = SqliteUserRepo(db)
        space_repo = SqliteSpaceRepo(db)
        user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
        await user_svc.provision(username="alice", display_name="Alice")
        svc = SpaceService(
            space_repo,
            SqliteSpacePostRepo(db),
            user_repo,
            bus,
            own_instance_id=iid,
        )
        gfs = AsyncMock()
        svc.attach_gfs_connection_service(gfs)
        yield svc, gfs
    finally:
        await db.shutdown()


async def test_create_global_space_publishes_to_all(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        lat=47.0,
        lon=8.0,
        space_type=SpaceType.GLOBAL,
    )
    gfs.publish_space_to_all.assert_awaited_once_with(space.id)
    gfs.unpublish_space_from_all.assert_not_awaited()


async def test_create_household_space_does_not_publish(stack):
    svc, gfs = stack
    await svc.create_space(
        owner_username="alice",
        name="Family",
        space_type=SpaceType.HOUSEHOLD,
    )
    gfs.publish_space_to_all.assert_not_awaited()


async def test_flip_household_to_global_publishes(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Family",
        space_type=SpaceType.HOUSEHOLD,
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        space_type=SpaceType.GLOBAL,
    )
    gfs.publish_space_to_all.assert_awaited_once_with(space.id)


async def test_flip_global_to_household_unpublishes(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess",
        lat=47.0,
        lon=8.0,
        space_type=SpaceType.GLOBAL,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        space_type=SpaceType.HOUSEHOLD,
    )
    gfs.unpublish_space_from_all.assert_awaited_once_with(space.id)


async def test_dissolve_global_space_unpublishes(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess",
        lat=47.0,
        lon=8.0,
        space_type=SpaceType.GLOBAL,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.dissolve_space(space.id, actor_username="alice")
    gfs.unpublish_space_from_all.assert_awaited_once_with(space.id)


async def test_update_without_type_change_does_nothing(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess",
        lat=47.0,
        lon=8.0,
        space_type=SpaceType.GLOBAL,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        name="Chess Renamed",
        join_mode=JoinMode.OPEN,
    )
    gfs.publish_space_to_all.assert_not_awaited()
    gfs.unpublish_space_from_all.assert_not_awaited()
