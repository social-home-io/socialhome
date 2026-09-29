"""Release-blocker protocol tests for ``@here`` in federated spaces.

Marked ``@pytest.mark.security``.

``@here`` pages every member of a space, so who may use it is a privilege.
It is decided on EVERY receiving household, from facts that household
already holds and a sender cannot forge:

* the space's ``allow_here_mention`` toggle as this household mirrors it;
* the author's seat role in this household's roster mirror
  (``space_remote_members``) — an ``admin`` seat, or the host's owner
  (seated on the host instance under the space's ``owner_username``).

Nothing in the post payload (a ``role`` / ``author_role`` claim, an
``allow_here_mention`` flag) is read. These tests drive the real inbound
handler and the real notification service over real SQLite and assert on
the notification rows a local member ends up with.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.user import RemoteUser
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
)
from socialhome.services.notification_service import NotificationService

pytestmark = pytest.mark.security

SP = "sp-here"
OWN = "this-household"
HOST = "the-host"
HOST_OWNER = "u-host-owner"  # the space owner, living on HOST
HOST_ADMIN = "u-host-admin"  # an admin seat on HOST
HOST_MEMBER = "u-host-member"  # a plain member seat on HOST
LOCAL = "u-local"  # our user, a member here, level "mentions"


async def _instance(db, instance_id):
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?, ?, ?, 'k1', 'k2',"
        " 'https://x/wh', ?, 'confirmed', 'manual')",
        (instance_id, instance_id, "00" * 32, f"wh-{instance_id}"),
    )


@pytest.fixture
async def stack(tmp_dir):
    db = AsyncDatabase(tmp_dir / "here.db", batch_timeout_ms=10)
    await db.startup()
    bus = EventBus()
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0d" * 32))
    users = SqliteUserRepo(db)
    remote = SqliteSpaceRemoteMemberRepo(db)
    notifs = SqliteNotificationRepo(db, max_per_user=50)
    await spaces.save(
        Space(
            id=SP,
            name="Salon",
            owner_instance_id=HOST,
            owner_username="hostowner",
            identity_public_key="00" * 32,
            config_sequence=1,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
            allow_here_mention=True,
        )
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("local", LOCAL, "Local"),
    )
    await spaces.save_member(
        SpaceMember(
            space_id=SP,
            user_id=LOCAL,
            role=SpaceRole.MEMBER.value,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )
    await notifs.set_space_notif_level(user_id=LOCAL, space_id=SP, level="mentions")
    await _instance(db, HOST)
    for user_id, username, role in (
        # The owner mirrors as a plain seat — a remote seat can't be "owner".
        (HOST_OWNER, "hostowner", SpaceRole.MEMBER.value),
        (HOST_ADMIN, "hostadmin", SpaceRole.ADMIN.value),
        (HOST_MEMBER, "hostmember", SpaceRole.MEMBER.value),
    ):
        await users.upsert_remote(
            RemoteUser(
                user_id=user_id,
                instance_id=HOST,
                remote_username=username,
                display_name=username.title(),
            )
        )
        await remote.add(
            space_id=SP,
            instance_id=HOST,
            user_id=user_id,
            user_pk=None,
            display_name=username.title(),
            role=role,
        )
    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=spaces,
        user_repo=users,
        space_remote_member_repo=remote,
    )
    inbound._federation_service = SimpleNamespace(own_instance_id=OWN)  # type: ignore[assignment]
    notif_svc = NotificationService(notifs, users, spaces, bus)
    notif_svc.wire()
    yield SimpleNamespace(
        db=db, spaces=spaces, remote=remote, notifs=notifs, inbound=inbound
    )
    await db.shutdown()


_n = iter(range(10_000))


async def _post(stack, author, content="@here meeting now", **extra):
    pid = f"p-{next(_n)}"
    await stack.inbound._on_space_post_created(
        FederationEvent(
            msg_id=f"m-{pid}",
            event_type=FederationEventType.SPACE_POST_CREATED,
            from_instance=HOST,
            to_instance=OWN,
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload={
                "id": pid,
                "author": author,
                "type": "text",
                "content": content,
                **extra,
            },
            space_id=SP,
        )
    )
    return pid


async def _bells(stack):
    notes = await stack.notifs.list(LOCAL, limit=50)
    return [n.type for n in notes if n.type.startswith("space_")]


async def test_remote_admin_seat_pages_members(stack):
    await _post(stack, HOST_ADMIN)
    assert await _bells(stack) == ["space_here"]


async def test_host_owner_pages_members(stack):
    await _post(stack, HOST_OWNER)
    assert await _bells(stack) == ["space_here"]


async def test_plain_member_here_is_ignored_even_with_a_role_claim(stack):
    """The receiver reads the seat, not the payload: a member's post that
    claims ``role: admin`` / ``author_role: owner`` still pages nobody."""
    await _post(
        stack,
        HOST_MEMBER,
        role="admin",
        author_role="owner",
        allow_here_mention=True,
    )
    assert await _bells(stack) == []


async def test_demoted_admin_loses_here(stack):
    """The role is read at receive time — a seat demoted to member (roster
    gossip) can no longer page anyone."""
    await stack.remote.set_role(SP, HOST, HOST_ADMIN, SpaceRole.MEMBER.value)
    await _post(stack, HOST_ADMIN)
    assert await _bells(stack) == []


async def test_toggle_off_means_nothing_even_for_the_owner(stack):
    space = await stack.spaces.get(SP)
    await stack.spaces.save(replace(space, allow_here_mention=False))
    await _post(stack, HOST_OWNER)
    await _post(stack, HOST_ADMIN)
    assert await _bells(stack) == []


async def test_owner_username_on_another_household_is_not_the_owner(stack):
    """A seat on a NON-host household whose username happens to equal the
    owner's is not the owner."""
    await _instance(stack.db, "house-other")
    await SqliteUserRepo(stack.db).upsert_remote(
        RemoteUser(
            user_id="u-fake-owner",
            instance_id="house-other",
            remote_username="hostowner",
            display_name="Fake",
        )
    )
    await stack.remote.add(
        space_id=SP,
        instance_id="house-other",
        user_id="u-fake-owner",
        user_pk=None,
        display_name="Fake",
    )
    await _post(stack, "u-fake-owner")
    assert await _bells(stack) == []
