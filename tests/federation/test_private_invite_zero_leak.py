"""§D1b zero-leak security test for SPACE_PRIVATE_INVITE.

Asserts that :meth:`SpaceService.invite_remote_user` never puts space
metadata in the plaintext envelope: ``send_event`` is called with
``space_id`` NOT set on the envelope (no plaintext ``space_id`` field)
and everything sensitive — space_id, invite_token, inviter info,
display hint — lives inside the encrypted payload only.

If a new plaintext field sneaks in here, this test fails. Any new
allow-listed plaintext field must be added to
:data:`_PLAINTEXT_ALLOW_LIST` below after a reviewer sign-off.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from social_home.crypto import derive_instance_id, generate_identity_keypair
from social_home.db.database import AsyncDatabase
from social_home.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from social_home.domain.space import JoinMode, SpaceType
from social_home.infrastructure.event_bus import EventBus
from social_home.repositories.space_post_repo import SqliteSpacePostRepo
from social_home.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from social_home.repositories.space_repo import SqliteSpaceRepo
from social_home.repositories.user_repo import SqliteUserRepo
from social_home.services.space_service import SpaceService
from social_home.services.user_service import UserService


# The only kwargs permitted on the send_event call for
# ``SPACE_PRIVATE_INVITE`` outbound. ``space_id`` is NOT in the list on
# purpose — for private invites it rides inside the encrypted payload.
_PLAINTEXT_ALLOW_LIST = frozenset(
    {
        "to_instance_id",
        "event_type",
        "payload",
    }
)


@pytest.fixture
async def space_stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "zero_leak.db", batch_timeout_ms=10)
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
        space = await svc.create_space(
            owner_username="alice",
            name="Chess Strategy",
            description="Invite-only tactics",
            emoji="♟",
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
        peer = RemoteInstance(
            id="peer-instance-id",
            display_name="Peer",
            remote_identity_pk=os.urandom(32).hex(),
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_webhook_url="https://peer.example",
            local_webhook_id="loc",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )
        federation_repo = MagicMock()
        federation_repo.get_instance = AsyncMock(return_value=peer)
        federation_service = MagicMock()
        federation_service.send_event = AsyncMock()
        svc.attach_federation(
            federation_service=federation_service,
            federation_repo=federation_repo,
            remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
        )
        yield svc, federation_service, federation_repo, space
    finally:
        await db.shutdown()


async def test_private_invite_envelope_has_no_space_id_in_plaintext(space_stack):
    svc, fed, _repo, space = space_stack
    token = await svc.invite_remote_user(
        space.id,
        actor_username="alice",
        invitee_instance_id="peer-instance-id",
        invitee_user_id="bob_user",
    )
    assert token

    fed.send_event.assert_awaited_once()
    kwargs = fed.send_event.await_args.kwargs

    # The zero-leak rule: no `space_id` on the envelope plaintext.
    assert "space_id" not in kwargs, (
        "space_id must not appear in send_event plaintext kwargs — "
        "§25.8.21 requires it to ride inside the encrypted payload."
    )
    for key in kwargs:
        assert key in _PLAINTEXT_ALLOW_LIST, (
            f"unexpected plaintext envelope field {key!r} — add to "
            "_PLAINTEXT_ALLOW_LIST only after reviewer sign-off."
        )
    payload = kwargs["payload"]
    assert payload["space_id"] == space.id
    assert payload["invite_token"] == token
    assert payload["inviter_user_id"]
    assert payload["space_display_hint"] == "Chess Strategy"
    assert payload["expires_at"]


async def test_private_invite_refuses_unpaired_peer(space_stack):
    svc, fed, repo, space = space_stack
    repo.get_instance = AsyncMock(return_value=None)
    with pytest.raises(Exception):
        await svc.invite_remote_user(
            space.id,
            actor_username="alice",
            invitee_instance_id="unknown",
            invitee_user_id="x",
        )
    fed.send_event.assert_not_awaited()
