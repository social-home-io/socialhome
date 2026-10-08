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
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceType,
    SpaceArchivedError,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
)
from socialhome.domain.user import User
from socialhome.repositories.space_remote_member_repo import SpaceRemoteMember
from socialhome.services.preferences_service import PreferencesService
from socialhome.services.system_chat_policy import (
    HOUSEHOLD_CHAT_SECTION,
    SPACE_CHAT_SECTION,
    HouseholdChatAccess,
    SpaceChatAccess,
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


async def test_household_chat_has_no_moderators(env):
    _, _, policy = env
    assert not await policy.may_moderate(_chat(), "u-anna")


async def test_may_moderate_is_false_without_rules_or_user(env):
    _, _, policy = env
    assert not await policy.may_moderate(_chat(SystemChatScope.SPACE), "u-anna")
    assert not await policy.may_moderate(_chat(None), "u-anna")
    assert not await policy.may_moderate(_chat(), "u-nobody")


# ── Space chat ─────────────────────────────────────────────────────────────


class _Spaces:
    """In-memory space repo: one space ``sp`` and its local seats."""

    def __init__(self) -> None:
        self.space: Space | None = Space(
            id="sp",
            name="S",
            owner_instance_id="host",
            owner_username="o",
            identity_public_key="ab" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
        self.members: dict[str, str] = {}
        self.banned: set[str] = set()

    async def get(self, space_id: str) -> Space | None:
        return self.space if self.space and space_id == self.space.id else None

    async def get_member(self, space_id: str, user_id: str) -> SpaceMember | None:
        role = self.members.get(user_id)
        if role is None:
            return None
        return SpaceMember(space_id=space_id, user_id=user_id, role=role, joined_at="")

    async def is_banned(self, space_id: str, user_id: str) -> bool:
        return user_id in self.banned

    async def list_bans(self, space_id: str) -> list[dict]:
        return [{"space_id": space_id, "user_id": u} for u in sorted(self.banned)]


@pytest.fixture
def space_env():
    spaces = _Spaces()
    access = SpaceChatAccess(spaces)  # type: ignore[arg-type]
    policy = SystemChatPolicy(_Users(ANNA, GONE, DELETED))  # type: ignore[arg-type]
    policy.register(SystemChatScope.SPACE, access)
    return spaces, policy


@pytest.mark.parametrize(
    "role", [SpaceRole.OWNER, SpaceRole.ADMIN, SpaceRole.MODERATOR, SpaceRole.MEMBER]
)
async def test_space_writers_read_and_write(space_env, role):
    spaces, policy = space_env
    spaces.members["u-anna"] = role.value
    assert await policy.can_read(_chat(SystemChatScope.SPACE), "u-anna")
    assert await policy.can_write(_chat(SystemChatScope.SPACE), "u-anna")


async def test_space_follower_neither_reads_nor_writes(space_env):
    spaces, policy = space_env
    spaces.members["u-anna"] = SpaceRole.SUBSCRIBER.value
    assert not await policy.can_read(_chat(SystemChatScope.SPACE), "u-anna")
    with pytest.raises(PermissionError):
        await policy.require(_chat(SystemChatScope.SPACE), "u-anna", write=False)


async def test_space_non_member_and_inactive_users_are_refused(space_env):
    spaces, policy = space_env
    spaces.members["u-gone"] = SpaceRole.MEMBER.value
    assert not await policy.can_read(_chat(SystemChatScope.SPACE), "u-anna")
    assert not await policy.can_read(_chat(SystemChatScope.SPACE), "u-gone")


async def test_space_banned_member_is_refused(space_env):
    spaces, policy = space_env
    spaces.members["u-anna"] = SpaceRole.MEMBER.value
    spaces.banned.add("u-anna")
    assert not await policy.can_read(_chat(SystemChatScope.SPACE), "u-anna")


@pytest.mark.parametrize("gone", ["dissolved", "unknown"])
async def test_space_dissolved_or_unknown_is_refused(space_env, gone):
    spaces, policy = space_env
    spaces.members["u-anna"] = SpaceRole.OWNER.value
    if gone == "dissolved":
        assert spaces.space is not None
        spaces.space = dataclasses.replace(spaces.space, dissolved=True)
    else:
        spaces.space = None
    with pytest.raises(PermissionError):
        await policy.require(_chat(SystemChatScope.SPACE), "u-anna", write=False)


async def test_space_chat_off_is_feature_disabled(space_env):
    spaces, policy = space_env
    spaces.members["u-anna"] = SpaceRole.MEMBER.value
    assert spaces.space is not None
    spaces.space = dataclasses.replace(spaces.space, features=SpaceFeatures(chat=False))
    with pytest.raises(FeatureDisabledError) as exc:
        await policy.require(_chat(SystemChatScope.SPACE), "u-anna", write=False)
    assert exc.value.section == SPACE_CHAT_SECTION
    assert not await policy.can_read(_chat(SystemChatScope.SPACE), "u-anna")


async def test_archived_space_chat_reads_but_refuses_writes(space_env):
    spaces, policy = space_env
    spaces.members["u-anna"] = SpaceRole.MEMBER.value
    assert spaces.space is not None
    spaces.space = dataclasses.replace(spaces.space, archived=True)
    assert await policy.can_read(_chat(SystemChatScope.SPACE), "u-anna")
    assert not await policy.can_write(_chat(SystemChatScope.SPACE), "u-anna")
    with pytest.raises(SpaceArchivedError):
        await policy.require(_chat(SystemChatScope.SPACE), "u-anna", write=True)


async def test_space_chat_default_level_is_mentions(space_env):
    _, policy = space_env
    assert policy.default_notif_level(_chat(SystemChatScope.SPACE)) == "mentions"


@pytest.mark.parametrize(
    ("role", "moderates"),
    [
        (SpaceRole.OWNER, True),
        (SpaceRole.ADMIN, True),
        (SpaceRole.MODERATOR, True),
        (SpaceRole.MEMBER, False),
        (SpaceRole.SUBSCRIBER, False),
    ],
)
async def test_space_content_authority_moderates(space_env, role, moderates):
    spaces, policy = space_env
    spaces.members["u-anna"] = role.value
    assert (
        await policy.may_moderate(_chat(SystemChatScope.SPACE), "u-anna") is moderates
    )


# ── Space chat: members on other households ────────────────────────────────


class _RemoteSeats:
    """In-memory ``space_remote_members`` (live rows only, like the repo)."""

    def __init__(self, *seats: SpaceRemoteMember) -> None:
        self.seats = list(seats)

    async def list_for_space(self, space_id: str) -> list[SpaceRemoteMember]:
        return [s for s in self.seats if s.space_id == space_id]


def _seat(user_id: str, role: str = "member", **kw) -> SpaceRemoteMember:
    return SpaceRemoteMember(
        space_id="sp",
        instance_id="peer-b",
        user_id=user_id,
        display_name=user_id.upper(),
        joined_at="2026-10-01T00:00:00+00:00",
        role=role,
        **kw,
    )


@pytest.fixture
def remote_env():
    spaces = _Spaces()
    seats = _RemoteSeats(
        _seat("r-member"),
        _seat("r-admin", role=SpaceRole.ADMIN.value),
        _seat("r-follower", role=SpaceRole.SUBSCRIBER.value),
        # A role the policy doesn't know is no writer (allow-list).
        _seat("r-unknown", role="guest"),
        _seat("r-gone", tombstoned=True),
        _seat("r-banned"),
        _seat("r-member"),  # a duplicate row counts once
    )
    spaces.banned.add("r-banned")
    access = SpaceChatAccess(spaces, seats)  # type: ignore[arg-type]
    policy = SystemChatPolicy(_Users(ANNA))  # type: ignore[arg-type]
    policy.register(SystemChatScope.SPACE, access)
    return spaces, policy


async def test_space_remote_seats_are_the_writers_on_other_households(remote_env):
    _spaces, policy = remote_env
    seats = await policy.remote_seats(_chat(SystemChatScope.SPACE))
    assert [s.user_id for s in seats] == ["r-member", "r-admin"]
    first = seats[0]
    assert first.conversation_id == "hh"
    assert first.instance_id == "peer-b"
    assert first.display_name == "R-MEMBER"
    # The roster carries no login: consumers look the person up by id.
    assert first.remote_username == ""


@pytest.mark.parametrize("state", ["chat_off", "dissolved", "unknown"])
async def test_space_remote_seats_empty_while_chat_unavailable(remote_env, state):
    spaces, policy = remote_env
    assert spaces.space is not None
    if state == "chat_off":
        spaces.space = dataclasses.replace(
            spaces.space, features=SpaceFeatures(chat=False)
        )
    elif state == "dissolved":
        spaces.space = dataclasses.replace(spaces.space, dissolved=True)
    else:
        spaces.space = None
    assert await policy.remote_seats(_chat(SystemChatScope.SPACE)) == []


async def test_remote_seats_empty_without_roster_household_or_rules(space_env):
    _spaces, policy = space_env
    # No remote-member repo wired.
    assert await policy.remote_seats(_chat(SystemChatScope.SPACE)) == []
    # No household rules registered → fail closed, no seats.
    assert await policy.remote_seats(_chat(SystemChatScope.HOUSEHOLD)) == []
    # A person-made conversation is no system chat.
    assert await policy.remote_seats(_chat(None)) == []


async def test_household_chat_has_no_remote_seats():
    access = HouseholdChatAccess(_Prefs())  # type: ignore[arg-type]
    assert await access.remote_seats(_chat()) == []
