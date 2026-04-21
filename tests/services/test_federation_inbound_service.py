"""Tests for :class:`FederationInboundService` — §24 inbound event dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from social_home.domain.events import (
    CommentAdded,
    DmMessageCreated,
    PostDeleted,
    SpacePostCreated,
    UserStatusChanged,
)
from social_home.domain.federation import FederationEvent, FederationEventType
from social_home.domain.post import Post, PostType
from social_home.domain.space import JoinMode, SpaceType
from social_home.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from social_home.services.federation_inbound_service import (
    FederationInboundService,
)


@pytest.fixture
async def inbound(db, bus):
    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    return service


def _event(event_type, payload, *, from_instance="peer-a", space_id=None):
    return FederationEvent(
        msg_id="msg-" + event_type.value,
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


# ─── DM ──────────────────────────────────────────────────────────────────


async def test_dm_message_persists_and_publishes_event(db, bus, inbound):
    # Seed conversation row so FK is satisfied
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    captured: list[DmMessageCreated] = []
    bus.subscribe(DmMessageCreated, captured.append)

    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-1",
                "sender_user_id": "user-remote",
                "sender_display_name": "Alice",
                "content": "hi",
                "recipient_user_ids": ["user-local"],
            },
        )
    )

    row = await db.fetchone(
        "SELECT id, content FROM conversation_messages WHERE id=?",
        ("m-1",),
    )
    assert row is not None
    assert row["content"] == "hi"
    assert len(captured) == 1
    assert captured[0].conversation_id == "conv-1"
    assert captured[0].recipient_user_ids == ("user-local",)


async def test_dm_message_missing_fields_drops(inbound):
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {"conversation_id": "conv-1"},  # missing message_id/sender_user_id
        )
    )
    # Nothing raised, nothing persisted — test passes when no exception


async def test_dm_message_deleted_soft_deletes(db, bus, inbound):
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-1",
                "sender_user_id": "user-remote",
                "content": "hi",
            },
        )
    )
    await inbound._on_dm_deleted(
        _event(
            FederationEventType.DM_MESSAGE_DELETED,
            {"message_id": "m-1"},
        )
    )
    row = await db.fetchone(
        "SELECT deleted FROM conversation_messages WHERE id=?",
        ("m-1",),
    )
    assert row["deleted"] == 1


# ─── Space posts ─────────────────────────────────────────────────────────


async def test_space_post_created_persists(db, bus, inbound):
    # Seed the space row — the space_post_repo has no FK back to spaces so
    # a minimal insert suffices for the v1 schema.
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-1",
            "Space 1",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    captured: list[SpacePostCreated] = []
    bus.subscribe(SpacePostCreated, captured.append)

    await inbound._on_space_post_created(
        _event(
            FederationEventType.SPACE_POST_CREATED,
            {
                "id": "post-1",
                "author": "user-remote",
                "type": "text",
                "content": "hello",
            },
            space_id="sp-1",
        )
    )
    row = await db.fetchone("SELECT id FROM space_posts WHERE id=?", ("post-1",))
    assert row is not None
    assert len(captured) == 1
    assert captured[0].post.id == "post-1"
    assert captured[0].space_id == "sp-1"


# ─── User status ─────────────────────────────────────────────────────────


async def test_user_status_updated_publishes_bus_event(bus, inbound):
    captured: list[UserStatusChanged] = []
    bus.subscribe(UserStatusChanged, captured.append)

    await inbound._on_user_status_updated(
        _event(
            FederationEventType.USER_STATUS_UPDATED,
            {"user_id": "u-1", "emoji": "🌴", "text": "On leave"},
        )
    )
    assert len(captured) == 1
    assert captured[0].user_id == "u-1"
    assert captured[0].status is not None
    assert captured[0].status.emoji == "🌴"


async def test_user_status_cleared_publishes_none(bus, inbound):
    captured: list[UserStatusChanged] = []
    bus.subscribe(UserStatusChanged, captured.append)

    await inbound._on_user_status_updated(
        _event(
            FederationEventType.USER_STATUS_UPDATED,
            {"user_id": "u-1", "status_cleared": True},
        )
    )
    assert captured[0].status is None


# ─── Remote users ────────────────────────────────────────────────────────


async def test_dm_reaction_add_and_remove(db, bus, inbound):
    """DM_MESSAGE_REACTION handles both action=add and action=remove."""
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-1",
                "sender_user_id": "user-remote",
                "content": "hi",
            },
        )
    )
    await inbound._on_dm_reaction(
        _event(
            FederationEventType.DM_MESSAGE_REACTION,
            {"message_id": "m-1", "user_id": "user-x", "emoji": "👍", "action": "add"},
        )
    )
    rows = await db.fetchall(
        "SELECT emoji FROM message_reactions WHERE message_id=?",
        ("m-1",),
    )
    assert [r["emoji"] for r in rows] == ["👍"]

    await inbound._on_dm_reaction(
        _event(
            FederationEventType.DM_MESSAGE_REACTION,
            {
                "message_id": "m-1",
                "user_id": "user-x",
                "emoji": "👍",
                "action": "remove",
            },
        )
    )
    rows = await db.fetchall(
        "SELECT emoji FROM message_reactions WHERE message_id=?",
        ("m-1",),
    )
    assert rows == []


async def test_space_post_updated_edits_content(db, bus, inbound):
    # Seed space + post
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-1",
            "Space 1",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    repo = SqliteSpacePostRepo(db)
    now = datetime.now(timezone.utc)
    await repo.save(
        "sp-1",
        Post(
            id="p-1",
            author="u",
            type=PostType.TEXT,
            created_at=now,
            content="old content",
        ),
    )

    await inbound._on_space_post_updated(
        _event(
            FederationEventType.SPACE_POST_UPDATED,
            {"id": "p-1", "content": "new content"},
            space_id="sp-1",
        )
    )
    row = await db.fetchone("SELECT content FROM space_posts WHERE id=?", ("p-1",))
    assert row["content"] == "new content"


async def test_space_post_deleted_soft_deletes_and_publishes(db, bus, inbound):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-1",
            "Space 1",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    repo = SqliteSpacePostRepo(db)
    await repo.save(
        "sp-1",
        Post(
            id="p-1",
            author="u",
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content="x",
        ),
    )
    captured: list[PostDeleted] = []
    bus.subscribe(PostDeleted, captured.append)

    await inbound._on_space_post_deleted(
        _event(
            FederationEventType.SPACE_POST_DELETED,
            {"post_id": "p-1", "moderated_by": "admin-a"},
            space_id="sp-1",
        )
    )
    row = await db.fetchone("SELECT content FROM space_posts WHERE id=?", ("p-1",))
    assert row["content"] is None
    assert len(captured) == 1


async def test_space_member_join_and_leave(db, inbound):
    # Seed space
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-1",
            "Space 1",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )

    await inbound._on_space_member_joined(
        _event(
            FederationEventType.SPACE_MEMBER_JOINED,
            {"user_id": "u-1", "role": "member"},
            space_id="sp-1",
        )
    )
    row = await db.fetchone(
        "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
        ("sp-1", "u-1"),
    )
    assert row["role"] == "member"

    await inbound._on_space_member_left(
        _event(
            FederationEventType.SPACE_MEMBER_LEFT,
            {"user_id": "u-1"},
            space_id="sp-1",
        )
    )
    row = await db.fetchone(
        "SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
        ("sp-1", "u-1"),
    )
    assert row is None


async def test_attach_registers_handlers_on_federation_service(db, bus):
    """attach_to wires the expected event types into the dispatcher."""
    registry = MagicMock()
    fake_federation = MagicMock()
    fake_federation._event_registry = registry

    service = FederationInboundService(
        bus=bus,
        conversation_repo=None,  # type: ignore[arg-type]
        space_post_repo=None,  # type: ignore[arg-type]
        space_repo=None,  # type: ignore[arg-type]
        user_repo=None,  # type: ignore[arg-type]
    )
    service.attach_to(fake_federation)

    registered_types = {call.args[0] for call in registry.register.call_args_list}
    assert FederationEventType.DM_MESSAGE in registered_types
    assert FederationEventType.DM_MESSAGE_DELETED in registered_types
    assert FederationEventType.DM_MESSAGE_REACTION in registered_types
    assert FederationEventType.SPACE_POST_CREATED in registered_types
    assert FederationEventType.SPACE_POST_UPDATED in registered_types
    assert FederationEventType.SPACE_POST_DELETED in registered_types
    assert FederationEventType.SPACE_COMMENT_CREATED in registered_types
    assert FederationEventType.SPACE_COMMENT_DELETED in registered_types
    assert FederationEventType.SPACE_MEMBER_JOINED in registered_types
    assert FederationEventType.SPACE_MEMBER_LEFT in registered_types
    assert FederationEventType.USERS_SYNC in registered_types
    assert FederationEventType.USER_UPDATED in registered_types
    assert FederationEventType.USER_REMOVED in registered_types
    assert FederationEventType.USER_STATUS_UPDATED in registered_types


async def test_dm_missing_conversation_is_noop(inbound):
    """Missing fields should silently drop rather than crash."""
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {},
        )
    )
    await inbound._on_dm_deleted(
        _event(
            FederationEventType.DM_MESSAGE_DELETED,
            {},
        )
    )
    await inbound._on_dm_reaction(
        _event(
            FederationEventType.DM_MESSAGE_REACTION,
            {},
        )
    )


async def test_user_removed_without_existing_remote_is_noop(inbound):
    """USER_REMOVED for an unknown user does not raise."""
    await inbound._on_user_removed(
        _event(
            FederationEventType.USER_REMOVED,
            {"user_id": "never-seen"},
        )
    )


async def test_space_comment_created_persists_and_publishes(db, bus, inbound):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-1",
            "Space 1",
            "peer-a",
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    repo = SqliteSpacePostRepo(db)
    await repo.save(
        "sp-1",
        Post(
            id="p-1",
            author="u",
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content="post",
        ),
    )
    captured: list[CommentAdded] = []
    bus.subscribe(CommentAdded, captured.append)

    await inbound._on_space_comment_added(
        _event(
            FederationEventType.SPACE_COMMENT_CREATED,
            {
                "post_id": "p-1",
                "comment_id": "c-1",
                "author": "u-r",
                "type": "text",
                "content": "nice",
            },
        )
    )
    row = await db.fetchone(
        "SELECT content FROM space_post_comments WHERE id=?",
        ("c-1",),
    )
    assert row["content"] == "nice"
    assert len(captured) == 1


async def test_space_report_inbound_persists_remote_report(db, bus):
    """Inbound SPACE_REPORT calls through to ReportService, landing a row
    with ``reporter_instance_id = event.from_instance``.
    """
    from social_home.repositories.report_repo import SqliteReportRepo
    from social_home.services.report_service import ReportService

    report_repo = SqliteReportRepo(db)
    report_service = ReportService(
        report_repo=report_repo,
        user_repo=SqliteUserRepo(db),
        bus=bus,
    )
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        report_service=report_service,
    )
    await svc._on_space_report(
        _event(
            FederationEventType.SPACE_REPORT,
            {
                "target_type": "post",
                "target_id": "p-remote",
                "category": "spam",
                "notes": "looks sketchy",
                "reporter_user_id": "u-remote",
            },
            from_instance="peer-a",
        )
    )
    rows = await db.fetchall(
        "SELECT reporter_user_id, reporter_instance_id, category FROM content_reports",
    )
    assert len(rows) == 1
    assert rows[0]["reporter_user_id"] == "u-remote"
    assert rows[0]["reporter_instance_id"] == "peer-a"
    assert rows[0]["category"] == "spam"


async def test_space_report_inbound_noop_without_report_service(db, bus):
    """If ReportService isn't attached, the handler logs + returns cleanly."""
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        report_service=None,
    )
    await svc._on_space_report(
        _event(
            FederationEventType.SPACE_REPORT,
            {
                "target_type": "post",
                "target_id": "p",
                "category": "spam",
                "reporter_user_id": "u",
            },
        )
    )
    rows = await db.fetchall("SELECT 1 FROM content_reports")
    assert rows == []


async def test_users_sync_upserts_remote_users(db, inbound):
    # remote_users has FK to remote_instances — seed the peer row first.
    await db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk,
               key_self_to_remote, key_remote_to_self,
               remote_webhook_url, local_webhook_id,
               status, source, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "peer-a",
            "Peer A",
            "aa" * 32,
            "enc",
            "enc",
            "https://peer/wh",
            "wh-peer-a",
            "confirmed",
            "manual",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    await inbound._on_users_sync(
        _event(
            FederationEventType.USERS_SYNC,
            {
                "users": [
                    {"user_id": "u-r1", "username": "alice", "display_name": "Alice"},
                    {"user_id": "u-r2", "username": "bob", "display_name": "Bob"},
                ]
            },
            from_instance="peer-a",
        )
    )
    rows = await db.fetchall(
        "SELECT user_id, instance_id, remote_username FROM remote_users ORDER BY user_id",
    )
    assert [r["user_id"] for r in rows] == ["u-r1", "u-r2"]
    assert rows[0]["instance_id"] == "peer-a"
    assert rows[0]["remote_username"] == "alice"
