"""§D1 auto-publish hook: flipping space_type to/from 'global' fans
publish / unpublish calls out to every active GFS connection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import JoinMode, SpaceFeatures, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


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
        from socialhome.infrastructure.key_manager import KeyManager

        user_repo = SqliteUserRepo(db)
        space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
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
    """A pure metadata edit (here, a rename) is not a reason to re-publish.
    Note this deliberately does NOT touch ``join_mode`` — that IS a reason,
    since the GFS enforces it on ``/gfs/subscribe`` (see below)."""
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
    )
    gfs.publish_space_to_all.assert_not_awaited()
    gfs.unpublish_space_from_all.assert_not_awaited()


async def test_locking_down_join_mode_republishes_to_the_gfs(stack):
    """Flipping a GLOBAL space's join mode must re-publish its metadata: the
    GFS stores it and shows it on the directory listing, so without this the
    directory would advertise the wrong way in until the owner's next WS
    reconnect."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.INVITE_ONLY,
    )
    gfs.publish_space_to_all.assert_awaited_once_with(space.id)
    gfs.unpublish_space_from_all.assert_not_awaited()


async def test_opening_join_mode_also_republishes(stack):
    """The other direction matters too."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.OPEN,
    )
    gfs.publish_space_to_all.assert_awaited_once_with(space.id)


async def test_unchanged_join_mode_does_not_republish(stack):
    """Re-submitting the same join mode is not a change — no publish."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.OPEN,
    )
    gfs.publish_space_to_all.assert_not_awaited()


async def test_join_mode_change_on_a_household_space_does_not_publish(stack):
    """A non-global space has nothing on any GFS to correct."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Family",
        space_type=SpaceType.HOUSEHOLD,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.OPEN,
    )
    gfs.publish_space_to_all.assert_not_awaited()


# ── allow_subscribers changes must reach the GFS too ─────────────────────


async def test_enabling_subscribers_republishes_to_the_gfs(stack):
    """Turning the readability opt-in ON must re-publish: until the GFS
    learns it, ``/gfs/subscribe`` keeps answering 403 for a space the owner
    has just opened up."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        features=SpaceFeatures(allow_subscribers=True),
    )
    gfs.publish_space_to_all.assert_awaited_once_with(space.id)
    gfs.unpublish_space_from_all.assert_not_awaited()


async def test_disabling_subscribers_republishes_and_purges(stack):
    """And turning it OFF, for the stronger reason: that publish is what
    PURGES the seats taken while the space was readable, so without it a
    subscriber would keep pulling relayed content until the owner's next WS
    reconnect."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        features=SpaceFeatures(allow_subscribers=True),
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        features=SpaceFeatures(allow_subscribers=False),
    )
    gfs.publish_space_to_all.assert_awaited_once_with(space.id)


async def test_unchanged_subscribers_flag_does_not_republish(stack):
    """A features edit that leaves the flag alone is a plain metadata edit."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        features=SpaceFeatures(allow_subscribers=True),
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        features=SpaceFeatures(allow_subscribers=True, gallery=False),
    )
    gfs.publish_space_to_all.assert_not_awaited()


async def test_one_republish_when_both_dials_move_together(stack):
    """Join mode and readability are folded into ONE condition, so changing
    both in a single PATCH re-publishes once, not twice."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.OPEN,
        features=SpaceFeatures(allow_subscribers=True),
    )
    gfs.publish_space_to_all.assert_awaited_once_with(space.id)


async def test_subscribers_flag_on_a_household_space_does_not_publish(stack):
    """A non-global space has nothing on any GFS to correct."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Family",
        space_type=SpaceType.HOUSEHOLD,
    )
    gfs.publish_space_to_all.reset_mock()
    await svc.update_config(
        space.id,
        actor_username="alice",
        features=SpaceFeatures(allow_subscribers=True),
    )
    gfs.publish_space_to_all.assert_not_awaited()
