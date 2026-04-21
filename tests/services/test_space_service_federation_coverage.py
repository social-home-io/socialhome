"""Coverage fill for :class:`SpaceService` federation-facing methods.

Covers remote invites (accept/decline), remote member removal, join
requests (approve/deny local + remote), and ``request_join_remote``.
Each test uses a MagicMock for FederationService so we never require
a real peer connection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from social_home.crypto import derive_instance_id, generate_identity_keypair
from social_home.db.database import AsyncDatabase
from social_home.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from social_home.domain.space import (
    JoinMode,
    SpacePermissionError,
    SpaceType,
)
from social_home.infrastructure.event_bus import EventBus
from social_home.repositories.space_post_repo import SqliteSpacePostRepo
from social_home.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from social_home.repositories.space_repo import SqliteSpaceRepo
from social_home.repositories.user_repo import SqliteUserRepo
from social_home.services.space_service import SpaceService
from social_home.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    svc = SpaceService(
        space_repo,
        SqliteSpacePostRepo(db),
        user_repo,
        bus,
        own_instance_id=iid,
    )
    fed_svc = MagicMock()
    fed_svc.send_event = AsyncMock()
    fed_repo = MagicMock()
    fed_repo.get_instance = AsyncMock(
        return_value=RemoteInstance(
            id="peer",
            display_name="Peer",
            remote_identity_pk="ab" * 32,
            key_self_to_remote="k",
            key_remote_to_self="k",
            remote_webhook_url="https://peer",
            local_webhook_id="l",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        ),
    )
    svc.attach_federation(
        federation_service=fed_svc,
        federation_repo=fed_repo,
        remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
    )

    class S:
        pass

    s = S()
    s.db = db
    s.svc = svc
    s.fed_svc = fed_svc
    s.fed_repo = fed_repo
    s.space_repo = space_repo
    s.user_svc = user_svc
    yield s
    await db.shutdown()


async def _user(stack, username):
    return await stack.user_svc.provision(
        username=username, display_name=username,
    )


# ── invite_remote_user ──────────────────────────────────────────────


async def test_invite_remote_user_rejects_unpaired_host(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    stack.fed_repo.get_instance.return_value = None
    with pytest.raises(SpacePermissionError):
        await stack.svc.invite_remote_user(
            space.id,
            actor_username="alicehost",
            invitee_instance_id="peer",
            invitee_user_id="bob",
        )


async def test_invite_remote_user_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="Private",
        space_type=SpaceType.PRIVATE,
    )
    token = await stack.svc.invite_remote_user(
        space.id,
        actor_username="alicehost",
        invitee_instance_id="peer",
        invitee_user_id="bob",
    )
    assert token
    stack.fed_svc.send_event.assert_awaited_once()


async def test_invite_remote_user_requires_federation():
    """Direct SpaceService without attach_federation must raise."""
    # Minimal setup: no federation.
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    svc = SpaceService.__new__(SpaceService)
    svc._federation = None
    svc._federation_repo = None
    with pytest.raises(RuntimeError):
        await svc.invite_remote_user(
            "sp",
            actor_username="alicehost",
            invitee_instance_id="peer",
            invitee_user_id="bob",
        )


# ── accept/decline_remote_invite ───────────────────────────────────


async def test_accept_remote_invite_unknown_token_raises(stack):
    with pytest.raises(KeyError):
        await stack.svc.accept_remote_invite(
            token="bogus", user_id="u",
        )


async def test_accept_remote_invite_not_cross_household_raises(stack):
    """A remote-invitation row saved with no remote_instance_id yields ValueError."""
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost", name="S",
    )
    # Directly insert a row without remote_instance_id.
    await stack.db.enqueue(
        """INSERT INTO space_invitations(
               id, space_id, invited_user_id, invited_by, remote_instance_id,
               remote_user_id, invite_token, status, expires_at
           ) VALUES(?, ?, 'u', 'x', '', 'u', 'local-tkn', 'pending',
                    datetime('now', '+1 day'))""",
        ("inv-1", space.id),
    )
    with pytest.raises(ValueError):
        await stack.svc.accept_remote_invite(
            token="local-tkn", user_id="u",
        )


async def test_accept_remote_invite_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost", name="S",
    )
    # Seed a remote invitation row.
    await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id="bob",
        invite_token="tok-xyz",
        space_display_hint="S",
    )
    # accept_remote_invite requires user_repo.get_by_id to return a user
    # — the SqliteUserRepo does not expose get_by_id (it's optional),
    # so the code path hits the hasattr(False) branch.
    await stack.svc.accept_remote_invite(
        token="tok-xyz", user_id="bob",
    )
    stack.fed_svc.send_event.assert_awaited()


async def test_decline_remote_invite_unknown_token(stack):
    with pytest.raises(KeyError):
        await stack.svc.decline_remote_invite(
            token="nope", user_id="u",
        )


async def test_decline_remote_invite_not_cross_household(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost", name="S",
    )
    await stack.db.enqueue(
        """INSERT INTO space_invitations(
               id, space_id, invited_user_id, invited_by, remote_instance_id,
               remote_user_id, invite_token, status, expires_at
           ) VALUES(?, ?, 'u', 'x', '', 'u', 'loc-dec', 'pending',
                    datetime('now', '+1 day'))""",
        ("inv-dec", space.id),
    )
    with pytest.raises(ValueError):
        await stack.svc.decline_remote_invite(
            token="loc-dec", user_id="u",
        )


async def test_decline_remote_invite_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost", name="S",
    )
    await stack.space_repo.save_remote_invitation(
        space_id=space.id,
        invited_by="alicehost-id",
        remote_instance_id="peer",
        remote_user_id="bob",
        invite_token="tok-decline",
        space_display_hint="S",
    )
    await stack.svc.decline_remote_invite(
        token="tok-decline", user_id="bob",
    )
    stack.fed_svc.send_event.assert_awaited()


# ── remove_remote_member ───────────────────────────────────────────


async def test_remove_remote_member_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost", name="S",
    )
    await stack.svc.remove_remote_member(
        space.id,
        actor_username="alicehost",
        instance_id="peer",
        user_id="bob",
    )
    stack.fed_svc.send_event.assert_awaited()


# ── request_join_remote ────────────────────────────────────────────


async def test_request_join_remote_requires_confirmed_peer(stack):
    stack.fed_repo.get_instance.return_value = None
    with pytest.raises(SpacePermissionError):
        await stack.svc.request_join_remote(
            "sp-remote",
            applicant_user_id="u",
            host_instance_id="unknown-peer",
        )


async def test_request_join_remote_happy(stack):
    await _user(stack, "alicehost")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        space_type=SpaceType.PUBLIC,
        lat=52.37,
        lon=4.89,
        radius_km=50,
    )
    rid = await stack.svc.request_join_remote(
        space.id,
        applicant_user_id="u-applicant",
        host_instance_id="peer",
        message="join",
    )
    assert rid
    stack.fed_svc.send_event.assert_awaited()


# ── on_remote_join_request_approved ────────────────────────────────


async def test_on_remote_join_request_approved_unknown_noop(stack):
    # No row for this request_id — the handler silently returns.
    await stack.svc.on_remote_join_request_approved(
        "missing", invite_token="x",
    )


async def test_on_remote_join_request_approved_happy(stack):
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobapp")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        space_type=SpaceType.PUBLIC,
        lat=52.37,
        lon=4.89,
        radius_km=50,
    )
    # First create the remote request (seeds space_join_requests row).
    rid = await stack.svc.request_join_remote(
        space.id,
        applicant_user_id=bob.user_id,
        host_instance_id="peer",
    )
    # Mint a token to consume.
    token = await stack.svc.create_invite_token(
        space.id, actor_username="alicehost",
    )
    # Handler auto-consumes it.
    await stack.svc.on_remote_join_request_approved(
        rid, invite_token=token,
    )


# ── approve_join_request / deny_join_request ──────────────────────


async def test_deny_local_join_request(stack):
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobrequester")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        join_mode=JoinMode.REQUEST,
    )
    rid = await stack.svc.request_join(
        space.id, user_id=bob.user_id, message="please",
    )
    await stack.svc.deny_join_request(rid, actor_username="alicehost")
    assert (
        await stack.space_repo.get_member(space.id, bob.user_id)
    ) is None


async def test_approve_local_join_request(stack):
    await _user(stack, "alicehost")
    bob = await _user(stack, "bobrequester")
    space = await stack.svc.create_space(
        owner_username="alicehost",
        name="S",
        join_mode=JoinMode.REQUEST,
    )
    rid = await stack.svc.request_join(
        space.id, user_id=bob.user_id,
    )
    member = await stack.svc.approve_join_request(
        rid, actor_username="alicehost",
    )
    assert member is not None
    assert member.user_id == bob.user_id


async def test_approve_unknown_request_raises(stack):
    await _user(stack, "alicehost")
    await stack.svc.create_space(owner_username="alicehost", name="S")
    with pytest.raises(KeyError):
        await stack.svc.approve_join_request(
            "missing-rid", actor_username="alicehost",
        )
