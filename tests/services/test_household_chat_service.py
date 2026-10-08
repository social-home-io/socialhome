"""Tests for socialhome.services.household_chat_service — and the household
chat end to end through DmService (policy-gated group-DM storage)."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import (
    MUTED_FOREVER,
    RemoteConversationMember,
    SystemChatScope,
)
from socialhome.domain.events import DmMessageCreated
from socialhome.domain.federation import FederationEventType
from socialhome.domain.preferences import FeatureDisabledError
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.preferences_repo import (
    HOUSEHOLD_ROW_ID,
    SqlitePreferencesRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.dm_service import DmService
from socialhome.services.household_chat_service import HouseholdChatService
from socialhome.services.preferences_service import PreferencesService
from socialhome.services.system_chat_policy import (
    HouseholdChatAccess,
    SystemChatPolicy,
)
from socialhome.services.user_service import UserService


class _Federation:
    def __init__(self) -> None:
        self.sent: list[tuple[str, FederationEventType]] = []

    async def send_event(self, *, to_instance_id, event_type, payload, **_kw):
        self.sent.append((to_instance_id, event_type))


class _GuardianBlocks:
    """§CP.F2 stand-in: ``pairs`` are guardian-blocked both ways."""

    def __init__(self) -> None:
        self.pairs: set[frozenset[str]] = set()
        self.protected: set[str] = set()

    def register_gate(self, _gate) -> None:
        pass

    async def is_protected(self, user_id: str) -> bool:
        return user_id in self.protected

    async def is_guardian_blocked(self, a: str, b: str) -> bool:
        return frozenset((a, b)) in self.pairs

    async def guardian_block_counterparts(self, user_id: str) -> frozenset[str]:
        return frozenset(
            other
            for pair in self.pairs
            if user_id in pair
            for other in pair - {user_id}
        )


@pytest.fixture
async def stack(tmp_dir):
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
    users = SqliteUserRepo(db)
    convos = SqliteConversationRepo(db)
    prefs_repo = SqlitePreferencesRepo(db)
    prefs = PreferencesService(prefs_repo)
    access = HouseholdChatAccess(prefs)
    federation = _Federation()
    dm = DmService(convos, users, bus, own_instance_id=iid)
    dm.attach_federation(federation, None, iid)  # type: ignore[arg-type]
    dm.attach_system_chats(SystemChatPolicy(users, household=access))
    guardian = _GuardianBlocks()
    dm.attach_child_protection(guardian)
    chat_svc = HouseholdChatService(convos, users, access, bus)
    chat_svc.wire()
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)

    class S:
        pass

    s = S()
    s.db, s.bus, s.users, s.convos = db, bus, users, convos
    s.prefs_repo, s.dm, s.chat, s.user_svc = prefs_repo, dm, chat_svc, user_svc
    s.federation, s.guardian = federation, guardian

    async def provision(username: str):
        return await user_svc.provision(username=username, display_name=username)

    async def feature(on: bool) -> None:
        await prefs_repo.ensure_row(HOUSEHOLD_ROW_ID)
        await prefs_repo.set_household_value("feat_household_chat", int(on))

    s.provision, s.feature = provision, feature
    yield s
    await db.shutdown()


async def _seated(s, chat_id: str) -> set[str]:
    return {
        m.username for m in await s.convos.list_members(chat_id) if not m.deleted_at
    }


# ── Lifecycle + reconciler ────────────────────────────────────────────────


async def test_summary_creates_the_chat_and_seats_every_active_user(stack):
    await stack.provision("anna")
    await stack.provision("bob")
    summary = await stack.chat.summary("anna")
    assert summary.enabled and summary.conversation_id
    assert summary.unread == 0 and summary.notif_level == "all"
    assert summary.muted_until is None
    chat = await stack.convos.get(summary.conversation_id)
    assert chat is not None and chat.system_scope is SystemChatScope.HOUSEHOLD
    assert await _seated(stack, chat.id) == {"anna", "bob"}
    again = await stack.chat.summary("bob")
    assert again.conversation_id == summary.conversation_id


async def test_reconciler_follows_user_provisioning_and_deprovisioning(stack):
    await stack.provision("anna")
    chat = await stack.chat.reconcile()
    await stack.provision("carl")
    assert await _seated(stack, chat.id) == {"anna", "carl"}
    await stack.user_svc.deprovision("carl")
    assert await _seated(stack, chat.id) == {"anna"}


async def test_events_before_the_chat_exists_create_nothing(stack):
    await stack.provision("anna")
    await stack.user_svc.deprovision("anna")
    assert await stack.convos.get_household_chat() is None


async def test_lazy_reconcile_takes_out_a_seat_no_event_removed(stack):
    await stack.provision("anna")
    await stack.provision("bob")
    chat = await stack.chat.reconcile()
    await stack.db.enqueue("UPDATE users SET state='inactive' WHERE username='bob'")
    await stack.chat.summary("anna")
    assert await _seated(stack, chat.id) == {"anna"}


async def test_summary_reports_unread_level_and_mute(stack):
    await stack.provision("anna")
    await stack.provision("bob")
    chat_id = (await stack.chat.summary("anna")).conversation_id
    await stack.dm.send_message(chat_id, sender_username="bob", content="hi all")
    await stack.dm.set_notif_level(chat_id, username="anna", level="mentions")
    await stack.dm.mute(chat_id, username="anna", duration="forever")
    summary = await stack.chat.summary("anna")
    assert summary.unread == 1
    assert summary.notif_level == "mentions"
    assert summary.muted_until == MUTED_FOREVER


async def test_summary_feature_off_creates_nothing(stack):
    await stack.provision("anna")
    await stack.feature(False)
    summary = await stack.chat.summary("anna")
    assert not summary.enabled and summary.conversation_id is None
    assert await stack.convos.get_household_chat() is None


async def test_summary_refuses_unknown_or_inactive_accounts(stack):
    with pytest.raises(PermissionError):
        await stack.chat.summary("nobody")
    await stack.provision("gone")
    await stack.db.enqueue("UPDATE users SET state='inactive' WHERE username='gone'")
    with pytest.raises(PermissionError):
        await stack.chat.summary("gone")


async def test_wire_without_a_bus_is_a_noop(stack):
    HouseholdChatService(stack.convos, stack.users, None, None).wire()  # type: ignore[arg-type]


# ── Through DmService ─────────────────────────────────────────────────────


async def test_members_read_send_edit_react_and_mark_read(stack):
    await stack.provision("anna")
    await stack.provision("bob")
    chat = await stack.chat.reconcile()
    events: list[DmMessageCreated] = []

    async def _on(e: DmMessageCreated) -> None:
        events.append(e)

    stack.bus.subscribe(DmMessageCreated, _on)
    msg = await stack.dm.send_message(chat.id, sender_username="anna", content="hi")
    assert events[-1].recipient_user_ids == ((await stack.users.get("bob")).user_id,)
    assert events[-1].system_scope == "household"
    assert [
        m.content for m in await stack.dm.list_messages(chat.id, reader_username="bob")
    ] == ["hi"]
    await stack.dm.edit_message(msg.id, editor_username="anna", new_content="hey")
    await stack.dm.add_reaction(msg.id, username="bob", emoji="👍")
    assert len(await stack.dm.list_reactions(msg.id, username="anna")) == 1
    await stack.dm.remove_reaction(msg.id, username="bob", emoji="👍")
    assert await stack.dm.count_unread(chat.id, username="bob") == 1
    await stack.dm.mark_read(chat.id, username="bob")
    assert await stack.dm.count_unread(chat.id, username="bob") == 0
    await stack.dm.delete_message(msg.id, actor_username="anna")
    assert (await stack.convos.get_message(msg.id)).deleted


async def test_a_user_without_a_seat_yet_is_seated_on_first_use(stack):
    await stack.provision("anna")
    chat = await stack.chat.reconcile()
    # Provisioned straight into the table: no event, no reconcile.
    await stack.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('dora','u-dora','Dora')"
    )
    await stack.dm.send_message(chat.id, sender_username="dora", content="me too")
    assert "dora" in await _seated(stack, chat.id)


async def test_non_household_accounts_are_refused(stack):
    await stack.provision("anna")
    await stack.provision("gone")
    chat = await stack.chat.reconcile()
    await stack.db.enqueue("UPDATE users SET state='inactive' WHERE username='gone'")
    for call in (
        stack.dm.list_messages(chat.id, reader_username="gone"),
        stack.dm.send_message(chat.id, sender_username="gone", content="x"),
        stack.dm.list_messages(chat.id, reader_username="stranger"),
    ):
        with pytest.raises(PermissionError):
            await call


async def test_without_a_policy_system_chats_fail_closed(stack):
    await stack.provision("anna")
    chat = await stack.chat.reconcile()
    bare = DmService(stack.convos, stack.users, stack.bus)
    with pytest.raises(PermissionError):
        await bare.list_messages(chat.id, reader_username="anna")


async def test_feature_off_refuses_reads_and_writes_but_keeps_the_data(stack):
    await stack.provision("anna")
    chat = await stack.chat.reconcile()
    msg = await stack.dm.send_message(chat.id, sender_username="anna", content="kept")
    await stack.feature(False)
    for call in (
        stack.dm.list_messages(chat.id, reader_username="anna"),
        stack.dm.send_message(chat.id, sender_username="anna", content="x"),
        stack.dm.edit_message(msg.id, editor_username="anna", new_content="y"),
        stack.dm.delete_message(msg.id, actor_username="anna"),
        stack.dm.add_reaction(msg.id, username="anna", emoji="👍"),
    ):
        with pytest.raises(FeatureDisabledError):
            await call
    await stack.feature(True)
    assert [
        m.content for m in await stack.dm.list_messages(chat.id, reader_username="anna")
    ] == ["kept"]


async def test_group_management_reads_as_not_found(stack):
    await stack.provision("anna")
    await stack.provision("bob")
    chat = await stack.chat.reconcile()
    bob = await stack.users.get("bob")
    for call in (
        stack.dm.leave(chat.id, username="anna"),
        stack.dm.rename_group(chat.id, actor_username="anna", name="x"),
        stack.dm.add_group_members(chat.id, actor_username="anna", usernames=["bob"]),
        stack.dm.remove_group_member(
            chat.id, actor_username="anna", user_id=bob.user_id
        ),
    ):
        with pytest.raises(KeyError):
            await call
    assert await _seated(stack, chat.id) == {"anna", "bob"}


async def test_the_household_chat_is_never_federated(stack):
    await stack.provision("anna")
    chat = await stack.chat.reconcile()
    # Even a (forged) remote seat never draws a DM event out.
    await stack.convos.add_remote_member(
        RemoteConversationMember(
            conversation_id=chat.id,
            instance_id="peer-x",
            remote_username="x",
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )
    msg = await stack.dm.send_message(chat.id, sender_username="anna", content="hi")
    await stack.dm.add_reaction(msg.id, username="anna", emoji="👍")
    await stack.dm.edit_message(msg.id, editor_username="anna", new_content="yo")
    await stack.dm.delete_message(msg.id, actor_username="anna")
    assert stack.federation.sent == []


async def test_guardian_block_never_refuses_but_hides_the_blocked_sender(stack):
    anna = await stack.provision("anna")  # protected account
    bob = await stack.provision("bob")  # blocked by anna's guardian
    await stack.provision("carl")
    chat = await stack.chat.reconcile()
    stack.guardian.protected.add(anna.user_id)
    stack.guardian.pairs.add(frozenset((anna.user_id, bob.user_id)))
    events: list[DmMessageCreated] = []

    async def _on(e: DmMessageCreated) -> None:
        events.append(e)

    stack.bus.subscribe(DmMessageCreated, _on)
    # Neither side is refused (a DM group would refuse both).
    from_bob = await stack.dm.send_message(chat.id, sender_username="bob", content="b")
    assert anna.user_id not in events[-1].recipient_user_ids
    await stack.dm.send_message(chat.id, sender_username="anna", content="a")
    assert bob.user_id not in events[-1].recipient_user_ids
    seen = [
        m.content for m in await stack.dm.list_messages(chat.id, reader_username="anna")
    ]
    assert seen == ["a"]
    assert {
        m.content for m in await stack.dm.list_messages(chat.id, reader_username="carl")
    } == {"a", "b"}
    # Anna can't react to a message she isn't shown.
    with pytest.raises(PermissionError):
        await stack.dm.add_reaction(from_bob.id, username="anna", emoji="👍")
