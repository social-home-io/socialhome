"""Tests for socialhome.services.system_chat_policy — live system-chat access."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from socialhome.domain.conversation import (
    Conversation,
    ConversationType,
    SystemChatScope,
)
from socialhome.domain.preferences import FeatureDisabledError, HouseholdPreferences
from socialhome.domain.user import User
from socialhome.services.preferences_service import PreferencesService
from socialhome.services.system_chat_policy import (
    HOUSEHOLD_CHAT_SECTION,
    HouseholdChatAccess,
    SystemChatPolicy,
)


class _Prefs:
    def __init__(self) -> None:
        self.household = HouseholdPreferences()

    async def get_household(self) -> HouseholdPreferences:
        return self.household


class _Users:
    def __init__(self, *users: User) -> None:
        self._by_id = {u.user_id: u for u in users}

    async def get_by_user_id(self, user_id: str) -> User | None:
        return self._by_id.get(user_id)


def _chat(scope: SystemChatScope | None = SystemChatScope.HOUSEHOLD) -> Conversation:
    return Conversation(
        id="hh",
        type=ConversationType.GROUP_DM,
        created_at=datetime.now(timezone.utc),
        system_scope=scope,
        space_id="sp" if scope is SystemChatScope.SPACE else None,
    )


ANNA = User(user_id="u-anna", username="anna", display_name="Anna")
GONE = User(user_id="u-gone", username="gone", display_name="Gone", state="inactive")
DELETED = User(
    user_id="u-del", username="del", display_name="Del", deleted_at="2026-01-01"
)


@pytest.fixture
def env():
    prefs = _Prefs()
    access = HouseholdChatAccess(PreferencesService(prefs))  # type: ignore[arg-type]
    policy = SystemChatPolicy(_Users(ANNA, GONE, DELETED), household=access)  # type: ignore[arg-type]
    return prefs, access, policy


async def test_active_local_user_reads_and_writes(env):
    _, _, policy = env
    assert await policy.can_read(_chat(), "u-anna")
    assert await policy.can_write(_chat(), "u-anna")
    user = await policy.require(_chat(), "u-anna", write=True)
    assert user.username == "anna"


@pytest.mark.parametrize("user_id", ["u-gone", "u-del", "u-remote", ""])
async def test_inactive_deleted_remote_or_unknown_users_are_refused(env, user_id):
    _, _, policy = env
    assert not await policy.can_read(_chat(), user_id)
    assert not await policy.can_write(_chat(), user_id)
    with pytest.raises(PermissionError):
        await policy.require(_chat(), user_id, write=False)


async def test_feature_off_refuses_everyone_with_feature_disabled(env):
    prefs, access, policy = env
    prefs.household = dataclasses.replace(prefs.household, feat_household_chat=False)
    assert not await access.enabled()
    assert not await policy.can_read(_chat(), "u-anna")
    with pytest.raises(FeatureDisabledError) as exc:
        await policy.require(_chat(), "u-anna", write=True)
    assert exc.value.section == HOUSEHOLD_CHAT_SECTION


async def test_a_scope_without_rules_fails_closed(env):
    _, _, policy = env
    space_chat = _chat(SystemChatScope.SPACE)
    assert not await policy.can_read(space_chat, "u-anna")
    with pytest.raises(PermissionError):
        policy.default_notif_level(space_chat)


async def test_a_plain_conversation_is_not_a_system_chat(env):
    _, _, policy = env
    with pytest.raises(PermissionError):
        await policy.require(_chat(None), "u-anna", write=False)


async def test_registered_scope_strategy_is_used(env):
    _, _, policy = env
    seen: list[tuple[str, bool]] = []

    class _SpaceAccess:
        default_notif_level = "mentions"

        async def check(self, conv, user, *, write):
            seen.append((user.user_id, write))
            if write:
                raise PermissionError("followers read only")

    policy.register(SystemChatScope.SPACE, _SpaceAccess())
    space_chat = _chat(SystemChatScope.SPACE)
    assert policy.default_notif_level(space_chat) == "mentions"
    assert await policy.can_read(space_chat, "u-anna")
    assert not await policy.can_write(space_chat, "u-anna")
    assert seen == [("u-anna", False), ("u-anna", True)]


async def test_household_default_level_is_all(env):
    _, _, policy = env
    assert policy.default_notif_level(_chat()) == "all"
