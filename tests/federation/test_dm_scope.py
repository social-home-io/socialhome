"""§24.11 — who may write into a direct conversation on behalf of whom.

Unit tests for :class:`DmScope` over in-memory stubs. The protocol-level
proof (real app, real registry, table snapshots) lives in
``tests/protocol/test_dm_scope.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from socialhome.domain.conversation import (
    Conversation,
    ConversationType,
    RemoteConversationMember,
    SystemChatScope,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.dm_scope import DmScope, refuse

US = "inst-us"
PEER = "inst-peer"
OTHER = "inst-other"


class _Users:
    def __init__(self) -> None:
        self.home = {"u-anna": US, "u-bob": PEER, "u-dora": OTHER}
        self.remote = {
            "u-bob": SimpleNamespace(instance_id=PEER, remote_username="bob"),
            "u-dora": SimpleNamespace(instance_id=OTHER, remote_username="dora"),
        }

    async def get_instance_for_user(self, user_id):
        return self.home.get(user_id)

    async def get_remote(self, user_id):
        return self.remote.get(user_id)


class _Conversations:
    def __init__(self) -> None:
        self.remote = {
            "c-bob": [RemoteConversationMember("c-bob", PEER, "bob", "t")],
            "c-dora": [RemoteConversationMember("c-dora", OTHER, "dora", "t")],
        }

    async def get(self, conversation_id):
        # Plain DMs only: no system chat (``DmScope`` asks).
        return None

    async def list_remote_members(self, conversation_id):
        return self.remote.get(conversation_id, [])


def _event(from_instance: str = PEER) -> FederationEvent:
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.DM_MESSAGE,
        from_instance=from_instance,
        to_instance=US,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={},
    )


def _scope() -> DmScope:
    return DmScope(conversation_repo=_Conversations(), user_repo=_Users())


async def test_sent_from_only_the_users_own_household():
    scope = _scope()
    assert await scope.sent_from(_event(), "u-bob")
    assert not await scope.sent_from(_event(), "u-anna")  # local member
    assert not await scope.sent_from(_event(), "u-dora")  # third household
    assert not await scope.sent_from(_event(), "u-ghost")  # unknown
    assert not await scope.sent_from(_event(), "")
    assert not await scope.sent_from(_event(""), "u-bob")


async def test_seated_needs_a_remote_seat_for_the_sender():
    scope = _scope()
    assert await scope.seated(_event(), "c-bob")
    assert not await scope.seated(_event(), "c-dora")
    assert not await scope.seated(_event(), "c-local-only")
    assert not await scope.seated(_event(), "")
    assert not await scope.seated(_event(""), "c-bob")


async def test_speaks_for_binds_user_household_and_seat():
    scope = _scope()
    assert await scope.speaks_for(_event(), "c-bob", "u-bob")
    assert not await scope.speaks_for(_event(), "c-dora", "u-bob")
    assert not await scope.speaks_for(_event(), "c-bob", "u-anna")
    assert not await scope.speaks_for(_event(OTHER), "c-bob", "u-dora")
    assert not await scope.speaks_for(_event(), "", "u-bob")


async def test_speaks_for_needs_the_remote_user_row():
    users = _Users()
    users.remote.pop("u-bob")
    scope = DmScope(conversation_repo=_Conversations(), user_repo=users)
    assert not await scope.speaks_for(_event(), "c-bob", "u-bob")


async def test_a_system_chat_seats_no_household_whatever_its_rows_say():
    """The household chat (or a space chat) is never a DM peer's: even a
    (forged) remote seat row binds nobody to it."""

    class _WithSystemChat(_Conversations):
        async def get(self, conversation_id):
            if conversation_id != "c-bob":
                return None
            return Conversation(
                id="c-bob",
                type=ConversationType.GROUP_DM,
                created_at=datetime.now(timezone.utc),
                system_scope=SystemChatScope.HOUSEHOLD,
            )

    scope = DmScope(conversation_repo=_WithSystemChat(), user_repo=_Users())
    assert not await scope.seated(_event(), "c-bob")
    assert await scope.seat_of(_event(), "c-bob", "u-bob") is None
    assert not await scope.speaks_for(_event(), "c-bob", "u-bob")


def test_refuse_logs_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="socialhome.federation.dm_scope"):
        refuse(_event(), "nope", message="m-1")
    assert "nope" in caplog.text
    assert "message=m-1" in caplog.text
