"""Tests for socialhome.services.dm_service."""

from __future__ import annotations

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import ConversationType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.dm_service import DmService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    """Full service stack for DM service tests."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    conv_repo = SqliteConversationRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    dm_svc = DmService(conv_repo, user_repo, bus)

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.user_svc = user_svc
    s.dm_svc = dm_svc

    async def provision_user(username, **kw):
        return await user_svc.provision(username=username, display_name=username, **kw)

    s.provision_user = provision_user
    yield s
    await db.shutdown()


async def test_1to1_dm(stack):
    """Creating a DM between two users is idempotent."""
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    assert dm.type is ConversationType.DM
    dm2 = await stack.dm_svc.create_dm(creator_username="bob", other_username="anna")
    assert dm2.id == dm.id


async def test_send_and_list(stack):
    """Messages sent to a DM are retrievable via list_messages."""
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")
    msgs = await stack.dm_svc.list_messages(dm.id, reader_username="anna")
    assert len(msgs) == 1 and msgs[0].content == "hi"


async def test_unread_and_mark_read(stack):
    """count_unread increments on new message; mark_read resets it to 0."""
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")
    assert await stack.dm_svc.count_unread(dm.id, username="bob") == 1
    await stack.dm_svc.mark_read(dm.id, username="bob")
    assert await stack.dm_svc.count_unread(dm.id, username="bob") == 0


async def test_edit_delete_sender_only(stack):
    """Only the sender can edit or delete their message."""
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    m = await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")
    await stack.dm_svc.edit_message(m.id, editor_username="anna", new_content="hey")
    with pytest.raises(PermissionError):
        await stack.dm_svc.edit_message(m.id, editor_username="bob", new_content="x")
    await stack.dm_svc.delete_message(m.id, actor_username="anna")
    with pytest.raises(PermissionError):
        await stack.dm_svc.delete_message(m.id, actor_username="bob")


async def test_group_dm(stack):
    """Creating a group DM produces a GROUP_DM conversation."""
    for name in ["anna", "bob", "carl"]:
        await stack.provision_user(name)
    gdm = await stack.dm_svc.create_group_dm(
        creator_username="anna",
        member_usernames=["bob", "carl"],
        name="Crew",
    )
    assert gdm.type is ConversationType.GROUP_DM


async def test_leave(stack):
    """Leaving a DM removes it from the user's conversation list."""
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    await stack.dm_svc.leave(dm.id, username="anna")
    convos = await stack.dm_svc.list_conversations("anna")
    assert dm.id not in {c.id for c in convos}


async def test_self_dm_rejected(stack):
    """Creating a DM to yourself raises ValueError."""
    await stack.provision_user("a")
    with pytest.raises(ValueError, match="yourself"):
        await stack.dm_svc.create_dm(creator_username="a", other_username="a")


async def test_send_bad_type(stack):
    """Sending with invalid message type raises ValueError."""
    await stack.provision_user("a")
    await stack.provision_user("b")
    dm = await stack.dm_svc.create_dm(creator_username="a", other_username="b")
    with pytest.raises(ValueError, match="invalid"):
        await stack.dm_svc.send_message(
            dm.id, sender_username="a", content="x", type="bogus"
        )


async def test_non_member_cannot_send(stack):
    """Non-member sending a message raises PermissionError."""
    await stack.provision_user("a")
    await stack.provision_user("b")
    await stack.provision_user("c")
    dm = await stack.dm_svc.create_dm(creator_username="a", other_username="b")
    with pytest.raises(PermissionError):
        await stack.dm_svc.send_message(dm.id, sender_username="c", content="x")


async def test_add_to_1on1_rejected(stack):
    """Adding a member to a 1:1 DM raises ValueError."""
    await stack.provision_user("a")
    await stack.provision_user("b")
    await stack.provision_user("c")
    dm = await stack.dm_svc.create_dm(creator_username="a", other_username="b")
    with pytest.raises(ValueError, match="1:1"):
        await stack.dm_svc.add_group_member(dm.id, actor_username="a", new_username="c")


async def test_edit_empty_content(stack):
    """Editing a message to empty content raises ValueError."""
    await stack.provision_user("a")
    await stack.provision_user("b")
    dm = await stack.dm_svc.create_dm(creator_username="a", other_username="b")
    m = await stack.dm_svc.send_message(dm.id, sender_username="a", content="hi")
    with pytest.raises(ValueError):
        await stack.dm_svc.edit_message(m.id, editor_username="a", new_content="")


# ─── §23.47 length cap ────────────────────────────────────────────────────


async def test_send_message_rejects_over_max_length(stack):
    from socialhome.services.dm_service import MAX_DM_LENGTH

    await stack.provision_user("a")
    await stack.provision_user("b")
    dm = await stack.dm_svc.create_dm(creator_username="a", other_username="b")
    too_long = "x" * (MAX_DM_LENGTH + 1)
    with pytest.raises(ValueError, match="exceeds"):
        await stack.dm_svc.send_message(
            dm.id,
            sender_username="a",
            content=too_long,
        )


async def test_send_message_at_max_length_succeeds(stack):
    from socialhome.services.dm_service import MAX_DM_LENGTH

    await stack.provision_user("a")
    await stack.provision_user("b")
    dm = await stack.dm_svc.create_dm(creator_username="a", other_username="b")
    msg = await stack.dm_svc.send_message(
        dm.id,
        sender_username="a",
        content="x" * MAX_DM_LENGTH,
    )
    assert len(msg.content) == MAX_DM_LENGTH


async def test_edit_message_rejects_over_max_length(stack):
    from socialhome.services.dm_service import MAX_DM_LENGTH

    await stack.provision_user("a")
    await stack.provision_user("b")
    dm = await stack.dm_svc.create_dm(creator_username="a", other_username="b")
    msg = await stack.dm_svc.send_message(
        dm.id,
        sender_username="a",
        content="hi",
    )
    with pytest.raises(ValueError, match="exceeds"):
        await stack.dm_svc.edit_message(
            msg.id,
            editor_username="a",
            new_content="x" * (MAX_DM_LENGTH + 1),
        )


# ─── §11/§13 outbound federation fan-out ──────────────────────────────────


class _FakeFederationService:
    """Collects send_event calls for outbound DM assertions."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
            }
        )


class _FakeFederationRepo:
    """Stand-in for AbstractFederationRepo — returns confirmed peers by id."""

    def __init__(self, peers):
        self._peers = peers

    async def get_instance(self, instance_id):
        return self._peers.get(instance_id)


def _confirmed_peer(instance_id: str):
    from types import SimpleNamespace

    from socialhome.domain.federation import PairingStatus

    return SimpleNamespace(id=instance_id, status=PairingStatus.CONFIRMED)


def _unconfirmed_peer(instance_id: str):
    from types import SimpleNamespace

    from socialhome.domain.federation import PairingStatus

    return SimpleNamespace(id=instance_id, status=PairingStatus.PENDING_SENT)


async def _attach_remote_member(stack, *, conversation_id, instance_id, username):
    from datetime import datetime, timezone

    from socialhome.domain.conversation import RemoteConversationMember

    await stack.dm_svc._convos.add_remote_member(  # noqa: SLF001
        RemoteConversationMember(
            conversation_id=conversation_id,
            instance_id=instance_id,
            remote_username=username,
            joined_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


async def test_send_federates_to_confirmed_peer(stack):
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-a": _confirmed_peer("peer-a")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id="self")

    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    await _attach_remote_member(
        stack,
        conversation_id=dm.id,
        instance_id="peer-a",
        username="bob",
    )
    await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")

    from socialhome.domain.federation import FederationEventType

    sent = [s for s in fed.sent if s["type"] == FederationEventType.DM_MESSAGE]
    assert len(sent) == 1
    assert sent[0]["to"] == "peer-a"
    assert sent[0]["payload"]["content"] == "hi"
    assert sent[0]["payload"]["conversation_id"] == dm.id


async def test_send_skips_unconfirmed_peer(stack):
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-a": _unconfirmed_peer("peer-a")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id="self")
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    await _attach_remote_member(
        stack,
        conversation_id=dm.id,
        instance_id="peer-a",
        username="bob",
    )
    await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")
    assert fed.sent == []


async def test_send_skips_own_instance(stack):
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"self": _confirmed_peer("self")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id="self")
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    await _attach_remote_member(
        stack,
        conversation_id=dm.id,
        instance_id="self",
        username="bob",
    )
    await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")
    assert fed.sent == []


async def test_edit_resends_dm_message_with_edited_at(stack):
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-a": _confirmed_peer("peer-a")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id="self")
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    await _attach_remote_member(
        stack,
        conversation_id=dm.id,
        instance_id="peer-a",
        username="bob",
    )
    msg = await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")
    fed.sent.clear()
    await stack.dm_svc.edit_message(msg.id, editor_username="anna", new_content="hey")
    assert len(fed.sent) == 1
    assert fed.sent[0]["payload"]["content"] == "hey"
    assert fed.sent[0]["payload"].get("edited_at")


async def test_delete_federates_deletion(stack):
    from socialhome.domain.federation import FederationEventType

    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-a": _confirmed_peer("peer-a")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id="self")
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    await _attach_remote_member(
        stack,
        conversation_id=dm.id,
        instance_id="peer-a",
        username="bob",
    )
    msg = await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")
    fed.sent.clear()
    await stack.dm_svc.delete_message(msg.id, actor_username="anna")
    dels = [s for s in fed.sent if s["type"] == FederationEventType.DM_MESSAGE_DELETED]
    assert len(dels) == 1
    assert dels[0]["payload"]["message_id"] == msg.id


async def test_reactions_federate_add_and_remove(stack):
    from socialhome.domain.federation import FederationEventType

    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-a": _confirmed_peer("peer-a")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id="self")
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    await _attach_remote_member(
        stack,
        conversation_id=dm.id,
        instance_id="peer-a",
        username="bob",
    )
    msg = await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")
    fed.sent.clear()
    anna = await stack.user_svc.get("anna")
    await stack.dm_svc.add_reaction(msg.id, user_id=anna.user_id, emoji="👍")
    await stack.dm_svc.remove_reaction(msg.id, user_id=anna.user_id, emoji="👍")
    reacts = [
        s for s in fed.sent if s["type"] == FederationEventType.DM_MESSAGE_REACTION
    ]
    assert [s["payload"]["action"] for s in reacts] == ["add", "remove"]
    assert all(s["payload"]["emoji"] == "👍" for s in reacts)


async def test_send_without_federation_attached_is_noop(stack):
    """DmService with no federation attached must still work locally."""
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    dm = await stack.dm_svc.create_dm(creator_username="anna", other_username="bob")
    # No remote member → no fan-out needed; and no federation service attached.
    msg = await stack.dm_svc.send_message(dm.id, sender_username="anna", content="hi")
    assert msg.content == "hi"


async def test_fan_out_mixes_confirmed_and_unconfirmed_peers(stack):
    """One confirmed + one unconfirmed peer → only the confirmed gets send_event."""
    fed = _FakeFederationService()
    repo = _FakeFederationRepo(
        {
            "peer-ok": _confirmed_peer("peer-ok"),
            "peer-new": _unconfirmed_peer("peer-new"),
        }
    )
    stack.dm_svc.attach_federation(fed, repo, own_instance_id="self")
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    await stack.provision_user("carl")
    gdm = await stack.dm_svc.create_group_dm(
        creator_username="anna",
        member_usernames=["bob", "carl"],
        name="Crew",
    )
    await _attach_remote_member(
        stack,
        conversation_id=gdm.id,
        instance_id="peer-ok",
        username="bob-ok",
    )
    await _attach_remote_member(
        stack,
        conversation_id=gdm.id,
        instance_id="peer-new",
        username="carl-new",
    )
    await stack.dm_svc.send_message(gdm.id, sender_username="anna", content="hi")
    from socialhome.domain.federation import FederationEventType

    sent = [s for s in fed.sent if s["type"] == FederationEventType.DM_MESSAGE]
    # Only the confirmed peer received the event. peer-new takes the DM
    # history sync path when it becomes reachable.
    assert {s["to"] for s in sent} == {"peer-ok"}


async def test_fan_out_deduplicates_same_instance(stack):
    """Two remote members on the same instance → one send_event call."""
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-a": _confirmed_peer("peer-a")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id="self")
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    await stack.provision_user("carl")
    gdm = await stack.dm_svc.create_group_dm(
        creator_username="anna",
        member_usernames=["bob", "carl"],
        name="Crew",
    )
    await _attach_remote_member(
        stack,
        conversation_id=gdm.id,
        instance_id="peer-a",
        username="bob-remote",
    )
    await _attach_remote_member(
        stack,
        conversation_id=gdm.id,
        instance_id="peer-a",
        username="carl-remote",
    )
    await stack.dm_svc.send_message(gdm.id, sender_username="anna", content="hi")
    from socialhome.domain.federation import FederationEventType

    sent = [s for s in fed.sent if s["type"] == FederationEventType.DM_MESSAGE]
    assert len(sent) == 1
    assert sent[0]["to"] == "peer-a"
