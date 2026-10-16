"""§D1 tier hook (``SpaceService.on_tier_change``): a space entering the
global tier is listed on exactly the GFSs its owner chose; one leaving it is
withdrawn from the GFSs that list it; a directory-setting change refreshes
only those listings; a public space never touches a GFS.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.errors import (
    GfsSelectionInvalidError,
    GfsSelectionRequiredError,
)
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
        selection = AsyncMock()

        async def _check(gfs_ids, *, required=False):
            ids = tuple(gfs_ids or ())
            if required and not ids:
                raise GfsSelectionRequiredError()
            return ids

        async def _still_active(gfs_ids):
            return tuple(g for g in gfs_ids if g != "gone")

        selection.check_selection.side_effect = _check
        selection.still_active.side_effect = _still_active
        svc.attach_gfs_server_selection(selection)
        # Attached to the GFS mock so one ``mock_calls`` shows every call.
        gfs.selection = selection
        yield svc, gfs
    finally:
        await db.shutdown()


def _gfs_publish_calls(gfs) -> list[str]:
    """Every GFS call that would list (or re-list) a space."""
    return [
        name
        for name, _args, _kw in gfs.mock_calls
        if name.startswith(("publish", "republish", "selection.apply"))
    ]


async def test_create_global_space_lists_on_the_chosen_gfs_only(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["g1", "g2"],
    )
    gfs.selection.check_selection.assert_awaited_once_with(["g1", "g2"], required=True)
    gfs.selection.apply.assert_awaited_once_with(space.id, ("g1", "g2"))
    gfs.unpublish_space_from_listed.assert_not_awaited()


async def test_create_global_space_needs_a_choice_before_anything_is_stored(stack):
    svc, gfs = stack
    with pytest.raises(GfsSelectionRequiredError):
        await svc.create_space(
            owner_username="alice", name="Chess", space_type=SpaceType.GLOBAL
        )
    assert await svc._spaces.list_by_type(SpaceType.GLOBAL) == []
    gfs.selection.apply.assert_not_awaited()


async def test_create_global_with_no_gfs_connected_is_global_not_listed(stack):
    """With no active connection the choice may be empty: the space is
    global, listed nowhere until its owner connects and picks one."""
    svc, gfs = stack

    async def _none(gfs_ids, *, required=False):
        return ()

    gfs.selection.check_selection.side_effect = _none
    space = await svc.create_space(
        owner_username="alice", name="Chess", space_type=SpaceType.GLOBAL
    )
    assert space.space_type is SpaceType.GLOBAL
    gfs.selection.apply.assert_not_awaited()


@pytest.mark.parametrize("space_type", [SpaceType.PUBLIC, SpaceType.HOUSEHOLD])
async def test_only_a_global_space_takes_servers(stack, space_type):
    svc, _gfs = stack
    with pytest.raises(GfsSelectionInvalidError) as exc_info:
        await svc.create_space(
            owner_username="alice",
            name="Club",
            space_type=space_type,
            gfs_ids=["g1"],
        )
    assert exc_info.value.params == {"reason": "not_global"}


@pytest.mark.security
async def test_a_public_space_never_contacts_a_gfs(stack):
    """Create, edit directory settings, flip to private and back: a public
    space is discoverable only by connected households."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Club",
        lat=47.0,
        lon=8.0,
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.INVITE_ONLY,
        features=SpaceFeatures(allow_subscribers=True),
        name="Club 2",
    )
    await svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.PRIVATE
    )
    await svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.PUBLIC
    )
    assert _gfs_publish_calls(gfs) == []
    gfs.selection.apply.assert_not_awaited()


async def test_create_household_space_does_not_publish(stack):
    svc, gfs = stack
    await svc.create_space(
        owner_username="alice",
        name="Family",
        space_type=SpaceType.HOUSEHOLD,
    )
    assert _gfs_publish_calls(gfs) == []


async def test_flip_household_to_global_lists_on_the_chosen_gfs(stack):
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
        gfs_ids=("g1", "gone"),
    )
    # A server disconnected since the choice is skipped, not an error.
    gfs.selection.apply.assert_awaited_once_with(space.id, ("g1",))


async def test_flip_to_global_without_a_choice_is_global_not_listed(stack):
    """A remote admin's approved proposal carries no servers: the space is
    global, listed nowhere, until the owner picks."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Family",
        space_type=SpaceType.HOUSEHOLD,
    )
    await svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.GLOBAL
    )
    gfs.selection.apply.assert_not_awaited()
    assert _gfs_publish_calls(gfs) == []


@pytest.mark.security
async def test_flip_global_to_household_withdraws_from_listing_gfs_only(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        space_type=SpaceType.HOUSEHOLD,
    )
    gfs.unpublish_space_from_listed.assert_awaited_once_with(space.id)
    assert [n for n, _a, _k in gfs.mock_calls if n.startswith("unpublish")] == [
        "unpublish_space_from_listed"
    ]


@pytest.mark.security
async def test_flip_global_to_public_withdraws_it_everywhere_it_was_listed(stack):
    """Public is never on a GFS: going back from global withdraws it."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.PUBLIC
    )
    gfs.unpublish_space_from_listed.assert_awaited_once_with(space.id)


async def test_dissolve_withdraws_from_listing_gfs(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["g1"],
    )
    await svc.dissolve_space(space.id, actor_username="alice")
    gfs.unpublish_space_from_listed.assert_awaited_once_with(space.id)


async def test_tier_change_of_a_space_owned_elsewhere_contacts_no_gfs(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice", name="Chess", space_type=SpaceType.HOUSEHOLD
    )
    # A stub of a space another household hosts (``save`` never moves the
    # owner, so set it directly).
    await svc._spaces._db.enqueue(
        "UPDATE spaces SET owner_instance_id='elsewhere' WHERE id=?", (space.id,)
    )
    await svc.on_tier_change(
        space.id, was_global=False, is_global=True, gfs_ids=("g1",)
    )
    await svc.on_tier_change(space.id, was_global=True, is_global=False)
    gfs.selection.apply.assert_not_awaited()
    gfs.unpublish_space_from_listed.assert_not_awaited()


async def test_update_without_type_change_does_nothing(stack):
    """A pure metadata edit (here, a rename) is not a reason to re-publish.
    Note this deliberately does NOT touch ``join_mode`` — that IS a reason,
    since the GFS enforces it on ``/gfs/subscribe`` (see below)."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        name="Chess Renamed",
    )
    gfs.republish_space.assert_not_awaited()
    gfs.unpublish_space_from_listed.assert_not_awaited()


@pytest.mark.security
async def test_locking_down_join_mode_refreshes_only_the_listing_gfs(stack):
    """Flipping a GLOBAL space's join mode must re-publish its metadata to
    the GFSs that list it (they show it on the directory) — and only to
    them: a settings edit is never how a GFS learns a space exists."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.INVITE_ONLY,
    )
    gfs.republish_space.assert_awaited_once_with(space.id)
    assert [n for n, _a, _k in gfs.mock_calls if n.startswith("publish")] == []


async def test_opening_join_mode_also_republishes(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.OPEN,
    )
    gfs.republish_space.assert_awaited_once_with(space.id)


async def test_unchanged_join_mode_does_not_republish(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.OPEN,
    )
    gfs.republish_space.assert_not_awaited()


async def test_join_mode_change_on_a_household_space_does_not_publish(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Family",
        space_type=SpaceType.HOUSEHOLD,
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.OPEN,
    )
    assert _gfs_publish_calls(gfs) == []


# ── allow_subscribers changes must reach the listing GFSs too ────────────


async def test_enabling_subscribers_republishes_to_the_listing_gfs(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        features=SpaceFeatures(allow_subscribers=True),
    )
    gfs.republish_space.assert_awaited_once_with(space.id)
    gfs.unpublish_space_from_listed.assert_not_awaited()


async def test_disabling_subscribers_republishes(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        features=SpaceFeatures(allow_subscribers=True),
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        features=SpaceFeatures(allow_subscribers=False),
    )
    gfs.republish_space.assert_awaited_once_with(space.id)


async def test_unchanged_subscribers_flag_does_not_republish(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        features=SpaceFeatures(allow_subscribers=True),
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        features=SpaceFeatures(allow_subscribers=True, gallery=False),
    )
    gfs.republish_space.assert_not_awaited()


async def test_one_republish_when_both_dials_move_together(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Chess Club",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        join_mode=JoinMode.OPEN,
        features=SpaceFeatures(allow_subscribers=True),
    )
    gfs.republish_space.assert_awaited_once_with(space.id)


async def test_subscribers_flag_on_a_household_space_does_not_publish(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Family",
        space_type=SpaceType.HOUSEHOLD,
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        features=SpaceFeatures(allow_subscribers=True),
    )
    assert _gfs_publish_calls(gfs) == []


async def test_public_space_turning_private_is_withdrawn_from_listing_gfs(stack):
    """A PUBLIC space hand-published before the owner model may still hold a
    listing; once private it must not stay listed. Only the GFSes that list
    it are told."""
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Club",
        lat=47.0,
        lon=8.0,
        space_type=SpaceType.PUBLIC,
    )
    await svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.PRIVATE
    )
    gfs.unpublish_space_from_listed.assert_awaited_once_with(space.id)


async def test_public_space_turning_household_is_withdrawn(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Club",
        lat=47.0,
        lon=8.0,
        space_type=SpaceType.PUBLIC,
    )
    await svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.HOUSEHOLD
    )
    gfs.unpublish_space_from_listed.assert_awaited_once_with(space.id)


async def test_public_space_turning_global_is_listed_on_the_choice(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Club",
        lat=47.0,
        lon=8.0,
        space_type=SpaceType.PUBLIC,
    )
    await svc.update_config(
        space.id,
        actor_username="alice",
        space_type=SpaceType.GLOBAL,
        gfs_ids=("g2",),
    )
    gfs.unpublish_space_from_listed.assert_not_awaited()
    gfs.selection.apply.assert_awaited_once_with(space.id, ("g2",))


async def test_global_space_turning_private_is_withdrawn_once(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Club",
        space_type=SpaceType.GLOBAL,
        gfs_ids=["g1"],
    )
    await svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.PRIVATE
    )
    gfs.unpublish_space_from_listed.assert_awaited_once_with(space.id)


async def test_private_space_turning_public_is_not_withdrawn(stack):
    svc, gfs = stack
    space = await svc.create_space(
        owner_username="alice",
        name="Club",
        lat=47.0,
        lon=8.0,
        space_type=SpaceType.PRIVATE,
    )
    await svc.update_config(
        space.id, actor_username="alice", space_type=SpaceType.PUBLIC
    )
    gfs.unpublish_space_from_listed.assert_not_awaited()
    assert _gfs_publish_calls(gfs) == []
