"""Tests for socialhome.services.dm_audience — one local fan-out rule."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationType,
    SystemChatScope,
)
from socialhome.domain.user import User
from socialhome.services.dm_audience import local_audience


class _Convos:
    def __init__(self, *, system: bool) -> None:
        self.system = system
        self.members = [
            ConversationMember(conversation_id="c", username=u, joined_at="t")
            for u in ("anna", "bob", "mia", "gone", "ghost")
        ] + [
            ConversationMember(
                conversation_id="c", username="left", joined_at="t", deleted_at="t"
            ),
            # A duplicate seat row never doubles a delivery.
            ConversationMember(conversation_id="c", username="bob", joined_at="t"),
        ]

    async def get(self, conversation_id):
        return Conversation(
            id="c",
            type=ConversationType.GROUP_DM,
            created_at=datetime.now(timezone.utc),
            system_scope=SystemChatScope.HOUSEHOLD if self.system else None,
        )

    async def list_members(self, conversation_id):
        return list(self.members)


class _Users:
    async def get(self, username):
        if username == "ghost":
            return None
        return User(
            user_id=f"u-{username}",
            username=username,
            display_name=username,
            state="inactive" if username == "gone" else "active",
        )


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        (True, ("u-bob",)),
        # A plain DM keeps its long-standing audience: removed seats and
        # inactive accounts are the DM code's business, not this rule's.
        (False, ("u-bob", "u-gone", "u-left")),
    ],
)
async def test_actor_and_withheld_are_left_out(system, expected):
    got = await local_audience(
        _Convos(system=system),
        _Users(),
        "c",
        actor_user_id="u-anna",
        withheld={"u-mia"},
    )
    assert got == expected


async def test_include_actor_keeps_the_actors_other_tabs():
    got = await local_audience(
        _Convos(system=True), _Users(), "c", actor_user_id="u-anna", include_actor=True
    )
    assert got == ("u-anna", "u-bob", "u-mia")
