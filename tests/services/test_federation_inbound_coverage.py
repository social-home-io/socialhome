"""Coverage fill for :class:`FederationInboundService`.

Exercises handlers that end-to-end tests leave untouched: comment
updated/deleted, member-profile updated, USER_* events, SPACE_REPORT
fallback. Each test calls the handler directly with a mock repo stack.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.domain.events import (
    CommentDeleted,
    CommentUpdated,
    SpaceMemberProfileUpdated,
    UserStatusChanged,
)
from socialhome.domain.post import Comment, CommentType
from socialhome.domain.space import SpaceMember
from socialhome.services.federation_inbound_service import FederationInboundService


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)


def _evt(
    event_type: str,
    payload: dict,
    *,
    from_instance: str = "peer-1",
    space_id: str | None = None,
):
    return SimpleNamespace(
        event_type=event_type,
        payload=payload,
        from_instance=from_instance,
        space_id=space_id,
    )


@pytest.fixture
def svc():
    bus = _RecordingBus()
    convo = AsyncMock()
    sp_post = AsyncMock()
    sp_repo = AsyncMock()
    user_repo = AsyncMock()
    report_svc = AsyncMock()
    s = FederationInboundService(
        bus=bus,  # type: ignore[arg-type]
        conversation_repo=convo,
        space_post_repo=sp_post,
        space_repo=sp_repo,
        user_repo=user_repo,
        report_service=report_svc,
    )
    return SimpleNamespace(
        svc=s,
        bus=bus,
        convo=convo,
        sp_post=sp_post,
        sp_repo=sp_repo,
        user_repo=user_repo,
        report_svc=report_svc,
    )


# ── DM ──────────────────────────────────────────────────────────────


async def test_dm_message_missing_fields_noops(svc):
    await svc.svc._on_dm_message(_evt("DM_MESSAGE", {}))
    svc.convo.save_message.assert_not_awaited()
    assert svc.bus.events == []


async def test_dm_message_bad_type_falls_back_to_text(svc):
    await svc.svc._on_dm_message(
        _evt(
            "DM_MESSAGE",
            {
                "conversation_id": "c",
                "message_id": "m",
                "sender_user_id": "u",
                "content": "hi",
                "type": "bogus",
                "recipient_user_ids": ["r1"],
            },
        ),
    )
    svc.convo.save_message.assert_awaited_once()


async def test_dm_deleted_missing_id_noops(svc):
    await svc.svc._on_dm_deleted(_evt("DM_MESSAGE_DELETED", {}))
    svc.convo.soft_delete_message.assert_not_awaited()


async def test_dm_deleted_happy_path(svc):
    await svc.svc._on_dm_deleted(
        _evt("DM_MESSAGE_DELETED", {"message_id": "m"}),
    )
    svc.convo.soft_delete_message.assert_awaited_once_with("m")


async def test_dm_reaction_missing_fields(svc):
    await svc.svc._on_dm_reaction(_evt("DM_MESSAGE_REACTION", {}))
    svc.convo.add_reaction.assert_not_awaited()


async def test_dm_reaction_add_and_remove(svc):
    await svc.svc._on_dm_reaction(
        _evt(
            "DM_MESSAGE_REACTION",
            {"message_id": "m", "user_id": "u", "emoji": "🎉"},
        ),
    )
    svc.convo.add_reaction.assert_awaited_once()
    await svc.svc._on_dm_reaction(
        _evt(
            "DM_MESSAGE_REACTION",
            {
                "message_id": "m",
                "user_id": "u",
                "emoji": "🎉",
                "action": "remove",
            },
        ),
    )
    svc.convo.remove_reaction.assert_awaited_once()


# ── Space post ──────────────────────────────────────────────────────


async def test_space_post_created_missing_space_noops(svc):
    await svc.svc._on_space_post_created(_evt("SPACE_POST_CREATED", {}))
    svc.sp_post.save.assert_not_awaited()


async def test_space_post_created_missing_author_noops(svc):
    await svc.svc._on_space_post_created(
        _evt("SPACE_POST_CREATED", {"id": "p"}, space_id="sp"),
    )
    svc.sp_post.save.assert_not_awaited()


async def test_space_post_updated_missing_id(svc):
    await svc.svc._on_space_post_updated(_evt("SPACE_POST_UPDATED", {}))
    svc.sp_post.edit.assert_not_awaited()


async def test_space_post_deleted_missing_id(svc):
    await svc.svc._on_space_post_deleted(_evt("SPACE_POST_DELETED", {}))
    svc.sp_post.soft_delete.assert_not_awaited()


async def test_space_post_deleted_happy_path(svc):
    await svc.svc._on_space_post_deleted(
        _evt(
            "SPACE_POST_DELETED",
            {"post_id": "p", "moderated_by": "admin"},
        ),
    )
    svc.sp_post.soft_delete.assert_awaited_once()


async def test_space_comment_added_missing_fields(svc):
    await svc.svc._on_space_comment_added(_evt("SPACE_COMMENT_CREATED", {}))
    svc.sp_post.add_comment.assert_not_awaited()


async def test_space_comment_added_bad_type_falls_back(svc):
    await svc.svc._on_space_comment_added(
        _evt(
            "SPACE_COMMENT_CREATED",
            {
                "post_id": "p",
                "comment_id": "c",
                "author": "u",
                "type": "bogus",
                "content": "hi",
            },
        ),
    )
    svc.sp_post.add_comment.assert_awaited_once()


async def test_space_comment_updated_missing_fields(svc):
    await svc.svc._on_space_comment_updated(_evt("SPACE_COMMENT_UPDATED", {}))
    svc.sp_post.edit_comment.assert_not_awaited()


async def test_space_comment_updated_happy_path(svc):
    from datetime import datetime, timezone

    fake_comment = Comment(
        id="c",
        post_id="p",
        author="u",
        type=CommentType.TEXT,
        created_at=datetime.now(timezone.utc),
        content="hi",
    )
    svc.sp_post.get_comment.return_value = fake_comment
    await svc.svc._on_space_comment_updated(
        _evt(
            "SPACE_COMMENT_UPDATED",
            {"comment_id": "c", "content": "new"},
            space_id="sp",
        ),
    )
    svc.sp_post.edit_comment.assert_awaited_once_with("c", "new")
    assert any(isinstance(e, CommentUpdated) for e in svc.bus.events)


async def test_space_comment_updated_missing_refreshed_noops(svc):
    svc.sp_post.get_comment.return_value = None
    await svc.svc._on_space_comment_updated(
        _evt(
            "SPACE_COMMENT_UPDATED",
            {"comment_id": "c", "content": "x"},
        ),
    )
    assert svc.bus.events == []


async def test_space_comment_deleted_missing_fields(svc):
    await svc.svc._on_space_comment_deleted(_evt("SPACE_COMMENT_DELETED", {}))
    svc.sp_post.soft_delete_comment.assert_not_awaited()


async def test_space_comment_deleted_happy_path(svc):
    await svc.svc._on_space_comment_deleted(
        _evt(
            "SPACE_COMMENT_DELETED",
            {"post_id": "p", "comment_id": "c"},
        ),
    )
    svc.sp_post.soft_delete_comment.assert_awaited_once()
    svc.sp_post.decrement_comment_count.assert_awaited_once()
    assert any(isinstance(e, CommentDeleted) for e in svc.bus.events)


# ── Space report ────────────────────────────────────────────────────


async def test_space_report_no_service_noops():
    """When report_service is None, handler logs and returns."""
    bus = _RecordingBus()
    s = FederationInboundService(
        bus=bus,  # type: ignore[arg-type]
        conversation_repo=AsyncMock(),
        space_post_repo=AsyncMock(),
        space_repo=AsyncMock(),
        user_repo=AsyncMock(),
        report_service=None,
    )
    await s._on_space_report(
        _evt("SPACE_REPORT", {"target_type": "post"}),
    )


async def test_space_report_with_service(svc):
    await svc.svc._on_space_report(
        _evt(
            "SPACE_REPORT",
            {
                "reporter_user_id": "r",
                "target_type": "post",
                "target_id": "p",
                "category": "spam",
                "notes": "x",
            },
        ),
    )
    svc.report_svc.create_report_from_remote.assert_awaited_once()


# ── Space membership ────────────────────────────────────────────────


async def test_space_member_joined_missing_fields(svc):
    await svc.svc._on_space_member_joined(_evt("SPACE_MEMBER_JOINED", {}))
    svc.sp_repo.save_member.assert_not_awaited()


async def test_space_member_joined_happy(svc):
    await svc.svc._on_space_member_joined(
        _evt(
            "SPACE_MEMBER_JOINED",
            {"user_id": "u", "role": "member"},
            space_id="sp",
        ),
    )
    svc.sp_repo.save_member.assert_awaited_once()


async def test_space_member_left_missing_fields(svc):
    await svc.svc._on_space_member_left(_evt("SPACE_MEMBER_LEFT", {}))
    svc.sp_repo.delete_member.assert_not_awaited()


async def test_space_member_left_happy(svc):
    await svc.svc._on_space_member_left(
        _evt("SPACE_MEMBER_LEFT", {"user_id": "u"}, space_id="sp"),
    )
    svc.sp_repo.delete_member.assert_awaited_once()


async def test_space_member_profile_updated_missing_fields(svc):
    await svc.svc._on_space_member_profile_updated(
        _evt("SPACE_MEMBER_PROFILE_UPDATED", {}),
    )
    svc.sp_repo.set_member_profile.assert_not_awaited()


async def test_space_member_profile_updated_unknown_member_noops(svc):
    svc.sp_repo.get_member.return_value = None
    await svc.svc._on_space_member_profile_updated(
        _evt(
            "SPACE_MEMBER_PROFILE_UPDATED",
            {"user_id": "u", "space_display_name": "X"},
            space_id="sp",
        ),
    )
    svc.sp_repo.set_member_profile.assert_not_awaited()


async def test_space_member_profile_updated_happy(svc):
    svc.sp_repo.get_member.return_value = SpaceMember(
        space_id="sp",
        user_id="u",
        role="member",
        joined_at="2020-01-01",
    )
    await svc.svc._on_space_member_profile_updated(
        _evt(
            "SPACE_MEMBER_PROFILE_UPDATED",
            {
                "user_id": "u",
                "space_display_name": "Alt",
                "picture_hash": "abcd",
            },
            space_id="sp",
        ),
    )
    svc.sp_repo.set_member_profile.assert_awaited_once()
    assert any(isinstance(e, SpaceMemberProfileUpdated) for e in svc.bus.events)


# ── User events ─────────────────────────────────────────────────────


async def test_users_sync_non_list_noops(svc):
    await svc.svc._on_users_sync(
        _evt("USERS_SYNC", {"users": "not-a-list"}),
    )
    svc.user_repo.upsert_remote.assert_not_awaited()


async def test_users_sync_happy(svc):
    await svc.svc._on_users_sync(
        _evt(
            "USERS_SYNC",
            {
                "users": [
                    {"user_id": "u1", "username": "u1", "display_name": "U1"},
                    {"user_id": "u2", "username": "u2"},
                ]
            },
        ),
    )
    assert svc.user_repo.upsert_remote.await_count == 2


async def test_user_updated_missing_fields_noops(svc):
    # No user_id / username → _upsert_remote_user early-returns.
    await svc.svc._on_user_updated(_evt("USER_UPDATED", {}))
    svc.user_repo.upsert_remote.assert_not_awaited()


async def test_user_updated_happy(svc):
    await svc.svc._on_user_updated(
        _evt(
            "USER_UPDATED",
            {
                "user_id": "u",
                "username": "user",
                "display_name": "User",
                "bio": "hi",
                "public_key": "pk",
            },
        ),
    )
    svc.user_repo.upsert_remote.assert_awaited_once()


async def test_user_removed_missing_id_noops(svc):
    await svc.svc._on_user_removed(_evt("USER_REMOVED", {}))
    svc.user_repo.mark_remote_deprovisioned.assert_not_awaited()


async def test_user_removed_happy(svc):
    await svc.svc._on_user_removed(
        _evt("USER_REMOVED", {"user_id": "u"}),
    )
    svc.user_repo.mark_remote_deprovisioned.assert_awaited_once_with("u")


async def test_user_status_missing_id_noops(svc):
    await svc.svc._on_user_status_updated(_evt("USER_STATUS_UPDATED", {}))
    assert svc.bus.events == []


async def test_user_status_cleared(svc):
    await svc.svc._on_user_status_updated(
        _evt(
            "USER_STATUS_UPDATED",
            {"user_id": "u", "status_cleared": True},
        ),
    )
    assert any(
        isinstance(e, UserStatusChanged) and e.status is None for e in svc.bus.events
    )


async def test_user_status_all_nil_emits_none(svc):
    # No emoji, no text, no cleared flag → status is None.
    await svc.svc._on_user_status_updated(
        _evt("USER_STATUS_UPDATED", {"user_id": "u"}),
    )
    assert any(
        isinstance(e, UserStatusChanged) and e.status is None for e in svc.bus.events
    )


async def test_user_status_populated(svc):
    await svc.svc._on_user_status_updated(
        _evt(
            "USER_STATUS_UPDATED",
            {
                "user_id": "u",
                "emoji": "🎉",
                "text": "Party",
                "expires_at": "2026-12-31T00:00:00Z",
            },
        ),
    )
    match = [e for e in svc.bus.events if isinstance(e, UserStatusChanged)]
    assert match and match[0].status is not None
    assert match[0].status.emoji == "🎉"


async def test_attach_to_registers_many_handlers(svc):
    """`attach_to` wires >= 16 event-type → handler bindings."""

    class _FakeRegistry:
        def __init__(self) -> None:
            self.bindings: dict = {}

        def register(self, event_type, handler):
            self.bindings[event_type] = handler

    class _FakeFedSvc:
        def __init__(self) -> None:
            self._event_registry = _FakeRegistry()

    fed = _FakeFedSvc()
    svc.svc.attach_to(fed)  # type: ignore[arg-type]
    assert len(fed._event_registry.bindings) >= 16
