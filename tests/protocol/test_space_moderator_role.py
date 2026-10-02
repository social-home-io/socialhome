"""Release-blocker protocol tests: the v_41 ``moderator`` seat holds
content authority and NO settings authority.

Marked ``@pytest.mark.security``.

The authority split is two sets in ``domain/space.py`` —
``SETTINGS_AUTHORITY_ROLES`` (owner / admin) and
``CONTENT_AUTHORITY_ROLES`` (owner / admin / moderator) — and every guard
reads one of them, locally and on the federated side
(:class:`SpaceAuthorship`). This file pins the whole matrix, one row per
role, over real SQLite repositories and the real services:

==============  ====  =========  ==========  ==========  ====  ====  =======  ========
role            zone  timetable  moderation  remote      kick  seed  role     settings
                                 delete      admin act         share change   PATCH
==============  ====  =========  ==========  ==========  ====  ====  =======  ========
owner           yes   yes        yes         n/a         n/a   n/a   yes      yes
admin           yes   yes        yes         yes         yes   yes   yes      yes
moderator       no    no         yes         no          no    no    no       no
member          no    no         no          no          no    no    no       no
subscriber      no    no         no          no          no    no    no       no
==============  ====  =========  ==========  ==========  ====  ====  =======  ========

The federated columns (zone, timetable, moderation delete) ask the
receiver's :class:`SpaceAuthorship` about a household holding that seat
(``owner`` = the host itself). The host-side columns (remote admin action,
kick, seed share) seat a remote household with that role on a space WE
host; ``owner`` has no remote seat shape, hence n/a. The local columns
(role change, settings) act as a local user holding the role.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import (
    RemoteAdminOutcome,
    SpaceFeatures,
    SpaceMember,
    SpacePermissionError,
    SpaceRole,
)
from socialhome.federation.space_authorship import SpaceAuthorship
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService

pytestmark = pytest.mark.security

ROLES = ("owner", "admin", "moderator", "member", "subscriber")
REMOTE_ROLES = ("admin", "moderator", "member", "subscriber")

#: role → (zone write, timetable write, moderation delete)
FEDERATED = {
    "owner": (True, True, True),
    "admin": (True, True, True),
    "moderator": (False, False, True),
    "member": (False, False, False),
    "subscriber": (False, False, False),
}
#: role → (remote admin action, kick, seed share)
HOST_SIDE = {
    "admin": (True, True, True),
    "moderator": (False, False, False),
    "member": (False, False, False),
    "subscriber": (False, False, False),
}
#: role → (role change member→moderator, settings PATCH)
LOCAL = {
    "owner": (True, True),
    "admin": (True, True),
    "moderator": (False, False),
    "member": (False, False),
    "subscriber": (False, False),
}

REMOTE_SP = "sp-remote"
HOST = "house-host"


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "mod.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x07" * 32))
    remote = SqliteSpaceRemoteMemberRepo(db)
    svc = SpaceService(spaces, SqliteSpacePostRepo(db), users, bus, own_instance_id=iid)
    svc._remote_members = remote
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)

    class S:
        pass

    s = S()
    s.db, s.svc, s.spaces, s.remote, s.users, s.user_svc = (
        db,
        svc,
        spaces,
        remote,
        users,
        user_svc,
    )
    s.authorship = SpaceAuthorship(
        space_repo=spaces, remote_member_repo=remote, user_repo=users
    )
    yield s
    await db.shutdown()


def _event(sender: str) -> FederationEvent:
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.SPACE_POST_DELETED,
        from_instance=sender,
        to_instance="us",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={},
        space_id=REMOTE_SP,
    )


# ── federated: the receiver's view of a household holding each seat ──


@pytest.mark.parametrize("role", ROLES)
async def test_federated_authority_per_role(stack, role):
    await stack.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (REMOTE_SP, "Remote", HOST, "hosty", "00" * 32),
    )
    await stack.remote.add(
        space_id=REMOTE_SP,
        instance_id="house-author",
        user_id="u-author",
        user_pk=None,
        display_name=None,
    )
    if role == "owner":
        sender, user = HOST, "u-owner"
        # The roster wire mirrors the owner as a plain member seat.
        await stack.remote.add(
            space_id=REMOTE_SP,
            instance_id=HOST,
            user_id=user,
            user_pk=None,
            display_name=None,
        )
    else:
        sender, user = f"house-{role}", f"u-{role}"
        await stack.remote.add(
            space_id=REMOTE_SP,
            instance_id=sender,
            user_id=user,
            user_pk=None,
            display_name=None,
            role=role,
        )
    ev = _event(sender)
    zone, timetable, moderation = FEDERATED[role]
    assert await stack.authorship.is_admin_household(ev, REMOTE_SP) is zone
    assert await stack.authorship.admin_as(ev, REMOTE_SP, user) is timetable
    assert await stack.authorship.may_mutate(ev, REMOTE_SP, "u-author") is moderation


# ── host side: a remote household seated with each role on OUR space ──


async def _hosted_with_remote(stack, role):
    await stack.user_svc.provision(username="anna", display_name="Anna")
    victim = await stack.user_svc.provision(username="vic", display_name="Vic")
    space = await stack.svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    await stack.svc.add_member(space.id, actor_username="anna", user_id=victim.user_id)
    await stack.remote.add(
        space_id=space.id,
        instance_id=f"house-{role}",
        user_id="u-remote",
        user_pk=None,
        display_name=None,
        role=role,
    )
    return space, victim


@pytest.mark.parametrize("role", REMOTE_ROLES)
async def test_remote_admin_action_per_role(stack, role):
    space, _victim = await _hosted_with_remote(stack, role)
    outcome = await stack.svc.apply_remote_admin_action(
        space.id,
        actor_instance_id=f"house-{role}",
        actor_user_id="u-remote",
        action="update_config",
        params={"name": "Changed"},
    )
    allowed = HOST_SIDE[role][0]
    assert (outcome is RemoteAdminOutcome.EXECUTED) is allowed
    assert ((await stack.spaces.get(space.id)).name == "Changed") is allowed


@pytest.mark.parametrize("role", REMOTE_ROLES)
async def test_remote_kick_per_role(stack, role):
    space, victim = await _hosted_with_remote(stack, role)
    await stack.svc.apply_remote_admin_kick(
        space.id,
        actor_instance_id=f"house-{role}",
        actor_user_id="u-remote",
        target_user_id=victim.user_id,
    )
    kicked = await stack.spaces.get_member(space.id, victim.user_id) is None
    assert kicked is HOST_SIDE[role][1]


@pytest.mark.parametrize("role", REMOTE_ROLES)
async def test_seed_share_audience_per_role(stack, role):
    """The delegated signing seed goes to ``list_admin_instances`` — the
    moderator household is never in it."""
    space, _victim = await _hosted_with_remote(stack, role)
    audience = await stack.remote.list_admin_instances(space.id)
    assert (f"house-{role}" in audience) is HOST_SIDE[role][2]


# ── local: a local user holding each role ──


async def _local_space(stack, role):
    await stack.user_svc.provision(username="anna", display_name="Anna")
    bob = await stack.user_svc.provision(username="bob", display_name="Bob")
    space = await stack.svc.create_space(owner_username="anna", name="S")
    await stack.svc.add_member(space.id, actor_username="anna", user_id=bob.user_id)
    if role == "owner":
        return space, "anna", bob
    actor = await stack.user_svc.provision(username="actor", display_name="Actor")
    await stack.spaces.save_member(
        SpaceMember(
            space_id=space.id,
            user_id=actor.user_id,
            role=role,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )
    return space, "actor", bob


@pytest.mark.parametrize("role", ROLES)
async def test_role_change_per_role(stack, role):
    space, actor, bob = await _local_space(stack, role)
    allowed = LOCAL[role][0]
    if allowed:
        await stack.svc.set_role(
            space.id, actor_username=actor, user_id=bob.user_id, role="moderator"
        )
    else:
        with pytest.raises(SpacePermissionError):
            await stack.svc.set_role(
                space.id, actor_username=actor, user_id=bob.user_id, role="moderator"
            )
    got = (await stack.spaces.get_member(space.id, bob.user_id)).role
    assert got == (SpaceRole.MODERATOR if allowed else SpaceRole.MEMBER)


@pytest.mark.parametrize("role", ROLES)
async def test_settings_patch_per_role(stack, role):
    space, actor, _bob = await _local_space(stack, role)
    allowed = LOCAL[role][1]
    if allowed:
        await stack.svc.update_config(space.id, actor_username=actor, name="Renamed")
    else:
        with pytest.raises(SpacePermissionError):
            await stack.svc.update_config(
                space.id, actor_username=actor, name="Renamed"
            )
    assert ((await stack.spaces.get(space.id)).name == "Renamed") is allowed


async def test_nobody_promotes_to_admin_but_the_owner(stack):
    """An admin may only move a seat between member and moderator."""
    space, _actor, bob = await _local_space(stack, "admin")
    with pytest.raises(SpacePermissionError):
        await stack.svc.set_role(
            space.id, actor_username="actor", user_id=bob.user_id, role="admin"
        )
    await stack.svc.set_role(
        space.id, actor_username="anna", user_id=bob.user_id, role="admin"
    )
    assert (await stack.spaces.get_member(space.id, bob.user_id)).role == "admin"
