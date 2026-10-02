"""Tests for socialhome.services.content_access (§4.3 feature access levels)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.space import (
    AccessAdminOnlyError,
    AccessDecision,
    ContentAction,
    ContentQueuedForReview,
    JoinMode,
    Space,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpaceMember,
    SpaceModerationItem,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.services.content_access import ContentAccessMixin


def _space(**access: SpaceFeatureAccess) -> Space:
    return Space(
        id="sp-1",
        name="S",
        owner_instance_id="inst-a",
        owner_username="anna",
        identity_public_key="aa" * 32,
        config_sequence=1,
        features=SpaceFeatures(**{f"{k}_access": v for k, v in access.items()}),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )


class _Spaces:
    def __init__(self, space: Space, roles: dict[str, str]) -> None:
        self.space = space
        self.roles = roles
        self.member_lookups = 0

    async def get(self, space_id: str) -> Space | None:
        return self.space if space_id == self.space.id else None

    async def get_member(self, space_id: str, user_id: str) -> SpaceMember | None:
        self.member_lookups += 1
        role = self.roles.get(user_id)
        if role is None or space_id != self.space.id:
            return None
        return SpaceMember(
            space_id=space_id, user_id=user_id, role=role, joined_at="2026-01-01"
        )


class _Consumer(ContentAccessMixin):
    __slots__ = ("_spaces",)

    def __init__(self, spaces: _Spaces) -> None:
        self._spaces = spaces


class _OtherSlotConsumer(ContentAccessMixin):
    """A consumer whose repo lives under another slot name."""

    __slots__ = ("_space_repo",)

    def __init__(self, spaces: _Spaces) -> None:
        self._space_repo = spaces

    def _access_space_repo(self):
        return self._space_repo


_ROLES = {
    "u-owner": SpaceRole.OWNER.value,
    "u-admin": SpaceRole.ADMIN.value,
    "u-mod": SpaceRole.MODERATOR.value,
    "u-member": SpaceRole.MEMBER.value,
    "u-sub": SpaceRole.SUBSCRIBER.value,
}


@pytest.mark.parametrize("feature", ["posts", "pages", "tasks", "stickies", "calendar"])
@pytest.mark.parametrize("action", list(ContentAction))
async def test_admin_only_denies_moderators_and_members(feature, action):
    spaces = _Spaces(_space(**{feature: SpaceFeatureAccess.ADMIN_ONLY}), _ROLES)
    svc = _Consumer(spaces)
    for user in ("u-owner", "u-admin"):
        got = await svc._gate("sp-1", user, feature, action, True)
        assert got is AccessDecision.PROCEED
    for user in ("u-mod", "u-member", "u-sub", "u-nobody"):
        with pytest.raises(AccessAdminOnlyError) as info:
            await svc._gate("sp-1", user, feature, action, True)
        assert info.value.feature == feature


async def test_open_proceeds_without_a_member_lookup():
    spaces = _Spaces(_space(), _ROLES)
    got = await _Consumer(spaces)._gate(
        spaces.space, "u-member", "tasks", ContentAction.CREATE, True
    )
    assert got is AccessDecision.PROCEED
    assert spaces.member_lookups == 0


async def test_accepts_a_space_object_or_an_id():
    spaces = _Spaces(_space(pages=SpaceFeatureAccess.ADMIN_ONLY), _ROLES)
    svc = _Consumer(spaces)
    with pytest.raises(AccessAdminOnlyError):
        await svc._gate(spaces.space, "u-member", "pages", ContentAction.EDIT, True)
    with pytest.raises(AccessAdminOnlyError):
        await svc._gate("sp-1", "u-member", "pages", ContentAction.EDIT, True)


async def test_unknown_space_is_not_found():
    svc = _Consumer(_Spaces(_space(), _ROLES))
    with pytest.raises(KeyError):
        await svc._gate("sp-404", "u-admin", "posts", ContentAction.CREATE, True)


async def test_moderated_posts_queue_for_a_member_create():
    spaces = _Spaces(_space(posts=SpaceFeatureAccess.MODERATED), _ROLES)
    got = await _Consumer(spaces)._gate(
        "sp-1", "u-member", "posts", ContentAction.CREATE, True
    )
    assert got is AccessDecision.QUEUE


@pytest.mark.parametrize("feature", ["pages", "tasks", "stickies", "calendar"])
async def test_moderated_non_post_features_queue_like_posts(feature):
    """Every MODERATED feature answers QUEUE for a member's create and edit /
    delete of somebody else's item; own edits and LAYOUT proceed."""
    spaces = _Spaces(_space(**{feature: SpaceFeatureAccess.MODERATED}), _ROLES)
    svc = _Consumer(spaces)
    assert (
        await svc._gate("sp-1", "u-member", feature, ContentAction.CREATE, True)
        is AccessDecision.QUEUE
    )
    assert (
        await svc._gate("sp-1", "u-member", feature, ContentAction.EDIT, False)
        is AccessDecision.QUEUE
    )
    assert (
        await svc._gate("sp-1", "u-member", feature, ContentAction.EDIT, True)
        is AccessDecision.PROCEED
    )
    assert (
        await svc._gate("sp-1", "u-member", feature, ContentAction.LAYOUT, False)
        is AccessDecision.PROCEED
    )
    assert (
        await svc._gate("sp-1", "u-mod", feature, ContentAction.DELETE, False)
        is AccessDecision.PROCEED
    )


class _RecordingSubmitter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def submit(self, space, **kw):
        self.calls.append(kw)
        return SpaceModerationItem(
            id="item-1",
            space_id=space.id,
            feature=kw["feature"],
            action=kw["action"].value,
            submitted_by=kw["submitted_by"],
            payload=kw["payload"],
            current_snapshot=None,
            submitted_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc),
        )

    async def require_moderation_supported(self, space, features) -> None:
        return None


class _QueueConsumer(ContentAccessMixin):
    __slots__ = ("_spaces", "_moderation")

    def __init__(self, spaces: _Spaces) -> None:
        self._spaces = spaces
        self._moderation = None


async def test_submit_for_review_raises_queued_with_the_item():
    spaces = _Spaces(_space(pages=SpaceFeatureAccess.MODERATED), _ROLES)
    svc = _QueueConsumer(spaces)
    sub = _RecordingSubmitter()
    svc.attach_moderation(sub)
    with pytest.raises(ContentQueuedForReview) as exc:
        await svc._submit_for_review(
            "sp-1",
            "u-member",
            "pages",
            ContentAction.CREATE,
            payload={"entity": "page"},
        )
    assert exc.value.item.id == "item-1"
    assert sub.calls[0]["submitted_by"] == "u-member"


async def test_submit_for_review_fails_closed_without_a_queue():
    spaces = _Spaces(_space(pages=SpaceFeatureAccess.MODERATED), _ROLES)
    with pytest.raises(SpacePermissionError):
        await _QueueConsumer(spaces)._submit_for_review(
            "sp-1", "u-member", "pages", ContentAction.CREATE, payload={}
        )


async def test_submit_for_review_unknown_space_is_404():
    spaces = _Spaces(_space(pages=SpaceFeatureAccess.MODERATED), _ROLES)
    svc = _QueueConsumer(spaces)
    svc.attach_moderation(_RecordingSubmitter())
    with pytest.raises(KeyError):
        await svc._submit_for_review(
            "nope", "u-member", "pages", ContentAction.CREATE, payload={}
        )


async def test_moderated_refuses_a_non_member_plainly():
    """A DENY that is not an ADMIN_ONLY one is a plain permission error."""
    spaces = _Spaces(_space(posts=SpaceFeatureAccess.MODERATED), _ROLES)
    with pytest.raises(SpacePermissionError) as info:
        await _Consumer(spaces)._gate(
            "sp-1", "u-nobody", "posts", ContentAction.CREATE, True
        )
    assert not isinstance(info.value, AccessAdminOnlyError)


async def test_a_consumer_may_name_its_own_repo_slot():
    spaces = _Spaces(_space(calendar=SpaceFeatureAccess.ADMIN_ONLY), _ROLES)
    svc = _OtherSlotConsumer(spaces)
    with pytest.raises(AccessAdminOnlyError):
        await svc._gate("sp-1", "u-mod", "calendar", ContentAction.CREATE, True)
    got = await svc._gate("sp-1", "u-admin", "calendar", ContentAction.CREATE, True)
    assert got is AccessDecision.PROCEED


def test_mixin_carries_no_slots():
    """Behaviour-only: composable with other slotted mixins."""
    assert ContentAccessMixin.__slots__ == ()
