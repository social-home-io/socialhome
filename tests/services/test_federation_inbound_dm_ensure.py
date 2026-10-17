"""Coverage for :meth:`FederationInboundService._ensure_remote_dm_conversation`.

Cross-household DMs arrive on instances that have never seen the
conversation locally. The inbound DM_MESSAGE handler must seat the
conversation row + the local recipient as a member + the remote
sender as a remote member before persisting the message.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.user import RemoteUser, User
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.services.federation_inbound_service import FederationInboundService


@pytest.fixture
async def inbound(db, bus):
    return FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )


async def _seed_local_user(db, user_repo, *, username: str, user_id: str) -> User:
    user = User(
        user_id=user_id,
        username=username,
        display_name=username.title(),
    )
    await user_repo.save(user)
    return user


async def _seed_remote_instance_and_user(
    db,
    user_repo,
    *,
    instance_id: str,
    remote_username: str,
    remote_user_id: str,
) -> RemoteUser:
    """Insert remote_instances + remote_users rows so the inbound
    handler can look the sender up via ``get_remote``.
    """
    await db.enqueue(
        """INSERT OR IGNORE INTO remote_instances(
               id, display_name, remote_identity_pk, key_self_to_remote,
               key_remote_to_self, remote_inbox_url, local_inbox_id,
               status, source
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            instance_id[:8],
            "00" * 32,
            "k1",
            "k2",
            "https://peer.example/federation/inbox/x",
            "local-inbox",
            "confirmed",
            "manual",
        ),
    )
    remote = RemoteUser(
        user_id=remote_user_id,
        instance_id=instance_id,
        remote_username=remote_username,
        display_name=remote_username.title(),
    )
    await user_repo.upsert_remote(remote)
    return remote


async def test_dm_message_auto_creates_conversation_on_receiver(db, bus, inbound):
    """A DM_MESSAGE for an unknown ``conversation_id`` must seed the
    conversation row + the local recipient's member row + the remote
    sender's member row, then save the message.
    """
    user_repo = SqliteUserRepo(db)
    conv_repo = SqliteConversationRepo(db)

    # Seed the local recipient (Carol) + the remote sender (Alice@peer-a).
    carol_user_id = "uid-carol-local"
    await _seed_local_user(
        db,
        user_repo,
        username="carol",
        user_id=carol_user_id,
    )
    alice_user_id = "uid-alice-remote"
    await _seed_remote_instance_and_user(
        db,
        user_repo,
        instance_id="peer-a",
        remote_username="alice",
        remote_user_id=alice_user_id,
    )

    conv_id = "conv-from-alice-to-carol"
    event = FederationEvent(
        msg_id="msg-1",
        event_type=FederationEventType.DM_MESSAGE,
        from_instance="peer-a",
        to_instance="self",
        timestamp=datetime.now(UTC).isoformat(),
        payload={
            "conversation_id": conv_id,
            "message_id": "msg-1",
            "sender_user_id": alice_user_id,
            "sender_display_name": "Alice",
            "type": "text",
            "content": "hi carol",
            "occurred_at": datetime.now(UTC).isoformat(),
            "recipient_user_ids": [carol_user_id],
        },
    )

    await inbound._on_dm_message(event)

    # Conversation row created.
    conv = await conv_repo.get(conv_id)
    assert conv is not None
    # Local recipient seated.
    members = await conv_repo.list_members(conv_id)
    assert "carol" in {m.username for m in members}
    # Remote sender seated.
    remotes = await conv_repo.list_remote_members(conv_id)
    assert ("peer-a", "alice") in {(m.instance_id, m.remote_username) for m in remotes}
    # Message persisted.
    msgs = await conv_repo.list_messages(conv_id, limit=10)
    assert any(m.content == "hi carol" for m in msgs)


async def test_dm_message_idempotent_on_existing_conversation(db, bus, inbound):
    """A second DM_MESSAGE for the same conversation must not blow up
    on the duplicate ``add_member`` / ``add_remote_member`` upserts.
    """
    user_repo = SqliteUserRepo(db)
    conv_repo = SqliteConversationRepo(db)
    carol_user_id = "uid-carol-local"
    await _seed_local_user(
        db,
        user_repo,
        username="carol",
        user_id=carol_user_id,
    )
    alice_user_id = "uid-alice-remote"
    await _seed_remote_instance_and_user(
        db,
        user_repo,
        instance_id="peer-a",
        remote_username="alice",
        remote_user_id=alice_user_id,
    )

    base_payload = {
        "conversation_id": "conv-x",
        "sender_user_id": alice_user_id,
        "sender_display_name": "Alice",
        "type": "text",
        "occurred_at": datetime.now(UTC).isoformat(),
        "recipient_user_ids": [carol_user_id],
    }

    for i in (1, 2, 3):
        event = FederationEvent(
            msg_id=f"msg-{i}",
            event_type=FederationEventType.DM_MESSAGE,
            from_instance="peer-a",
            to_instance="self",
            timestamp=datetime.now(UTC).isoformat(),
            payload={
                **base_payload,
                "message_id": f"msg-{i}",
                "content": f"msg #{i}",
            },
        )
        await inbound._on_dm_message(event)

    msgs = await conv_repo.list_messages("conv-x", limit=10)
    assert len(msgs) == 3
    members = await conv_repo.list_members("conv-x")
    # Only one local member row even after three calls.
    assert len([m for m in members if m.username == "carol"]) == 1
