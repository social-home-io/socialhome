"""Tests for :class:`FederationInboundService` — §24 inbound event dispatch."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from socialhome.domain.events import (
    CommentAdded,
    DmMessageCreated,
    PostDeleted,
    SpaceConfigChanged,
    SpacePostCreated,
    UserStatusChanged,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import JoinMode, SpaceMember, SpaceType
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo
from socialhome.services.federation_inbound_service import (
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


def _event(
    event_type, payload, *, from_instance="peer-a", space_id=None, media_bytes=None
):
    return FederationEvent(
        msg_id="msg-" + event_type.value,
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
        media_bytes=media_bytes,
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


async def test_dm_message_duplicate_transport_does_not_publish_created_twice(
    db,
    bus,
    inbound,
):
    """Second arrival of the same envelope (e.g. WebRTC delivers, then
    HTTPS-inbox redelivers) must NOT republish ``DmMessageCreated`` —
    otherwise the user gets two bell rows + two pushes for one
    message. Race-safety is at the repo layer; this test pins the
    end-to-end contract that the inbound handler honours it."""
    from socialhome.domain.events import DmMessageUpdated

    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-dup", "dm"),
    )
    created: list[DmMessageCreated] = []
    updated: list[DmMessageUpdated] = []
    bus.subscribe(DmMessageCreated, created.append)
    bus.subscribe(DmMessageUpdated, updated.append)

    payload = {
        "conversation_id": "conv-dup",
        "message_id": "m-dup",
        "sender_user_id": "user-remote",
        "sender_display_name": "Alice",
        "content": "hi",
        "recipient_user_ids": ["user-local"],
    }
    await inbound._on_dm_message(_event(FederationEventType.DM_MESSAGE, payload))
    await inbound._on_dm_message(_event(FederationEventType.DM_MESSAGE, payload))

    # First arrival fired Created; second arrival is a silent no-op
    # (no Created, and no Updated either — there was no real edit).
    assert len(created) == 1
    assert len(updated) == 0


async def test_dm_message_edit_replay_publishes_updated(db, bus, inbound):
    """A genuine edit (payload carries ``edited_at``) on an existing
    row still publishes ``DmMessageUpdated`` so the SPA can patch
    the bubble in place. The audit pass that suppresses the no-op
    updated-publish for duplicate-transport replays must not also
    suppress legitimate edits."""
    from socialhome.domain.events import DmMessageUpdated

    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-edit", "dm"),
    )
    created: list[DmMessageCreated] = []
    updated: list[DmMessageUpdated] = []
    bus.subscribe(DmMessageCreated, created.append)
    bus.subscribe(DmMessageUpdated, updated.append)

    # First delivery — brand-new message.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-edit",
                "message_id": "m-edit",
                "sender_user_id": "user-remote",
                "sender_display_name": "Alice",
                "content": "original",
                "recipient_user_ids": ["user-local"],
            },
        )
    )
    # Sender re-fans the envelope with edited content + an
    # ``edited_at`` timestamp — the signal that this is a real edit.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-edit",
                "message_id": "m-edit",
                "sender_user_id": "user-remote",
                "sender_display_name": "Alice",
                "content": "edited content",
                "recipient_user_ids": ["user-local"],
                "edited_at": "2026-05-21T12:00:00+00:00",
            },
        )
    )

    assert len(created) == 1
    assert len(updated) == 1
    assert updated[0].content == "edited content"
    row = await db.fetchone(
        "SELECT content FROM conversation_messages WHERE id=?",
        ("m-edit",),
    )
    assert row is not None
    assert row["content"] == "edited content"


async def test_dm_message_missing_fields_drops(inbound):
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {"conversation_id": "conv-1"},  # missing message_id/sender_user_id
        )
    )
    # Nothing raised, nothing persisted — test passes when no exception


async def test_dm_message_lazy_creates_conversation_before_seq_record(db, bus):
    """Regression: DM_MESSAGE for an unknown conversation must not trip
    a FOREIGN KEY constraint on ``conversation_sender_sequences``.

    Pre-fix, ``_on_dm_message`` called ``record_received_seq`` (which
    FK-references ``conversations(id)``) *before* the
    ``_ensure_remote_dm_conversation`` lazy-create that builds the
    conversation row on the receiver. On a cross-household first send
    the receiver had no row yet → ``sqlite3.IntegrityError: FOREIGN
    KEY constraint failed`` → the handler crashed → the message never
    landed in ``/api/conversations``. Federation-demo's ``verify`` step
    surfaced this on the a→c DM check.

    After the fix: the conversation row is upserted up-front so the
    sequence record always finds its FK target.
    """
    routing_repo = SqliteDmRoutingRepo(db)
    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        dm_routing_repo=routing_repo,
    )

    # Deliberately do NOT pre-seed the conversation row. The handler
    # must lazy-create it.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-fresh",
                "message_id": "m-1",
                "sender_user_id": "user-remote",
                "content": "first contact",
                # Seq must be carried so the sequence-record path
                # (which is where the FK trips) actually runs.
                "sender_seq": 1,
                "recipient_user_ids": ["user-local"],
            },
        ),
    )

    # The conversation row was created on the receiver.
    conv_rows = await db.fetchall(
        "SELECT id FROM conversations WHERE id = ?",
        ("conv-fresh",),
    )
    assert conv_rows, "conversation row should be lazy-created by _on_dm_message"

    # The sequence row landed (no FK violation; watermark = 1).
    assert (
        await routing_repo.peek_sender_seq(
            conversation_id="conv-fresh",
            sender_user_id="user-remote",
        )
        == 1
    )


async def test_dm_message_first_inbound_sweeps_bogus_gaps(db, bus):
    """End-to-end regression for the receiver-side high-watermark fix.

    Pre-fix, the gap detector never advanced its high-watermark on the
    inbound path, so every message after the first re-tripped a bogus
    ``missing=1..N-1`` gap that landed in ``conversation_message_gaps``.
    The SPA's DmThreadPage renders those rows as a "X messages may be
    missing" banner.

    Repro shape:
      * pre-existing bogus gap rows for the (conv, sender) pair
      * ``conversation_sender_sequences`` row is empty (watermark = 0)
      * inbound ``DM_MESSAGE`` arrives with ``sender_seq=5``

    After the fix: gap rows are swept, watermark is seeded to 5, no new
    gap is inserted.
    """
    routing_repo = SqliteDmRoutingRepo(db)
    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        dm_routing_repo=routing_repo,
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    # Pre-seed the bogus rows the pre-fix detector would have inserted.
    await routing_repo.insert_gaps(
        conversation_id="conv-1",
        sender_user_id="user-remote",
        expected_seqs=[1, 2, 3, 4],
    )
    assert len(await routing_repo.list_open_gaps("conv-1")) == 4

    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-5",
                "sender_user_id": "user-remote",
                "content": "hi",
                "sender_seq": 5,
            },
        ),
    )

    # All bogus rows for (conv-1, user-remote) are gone.
    assert await routing_repo.list_open_gaps("conv-1") == []
    # Watermark seeded to the first observed seq.
    assert (
        await routing_repo.peek_sender_seq(
            conversation_id="conv-1",
            sender_user_id="user-remote",
        )
        == 5
    )

    # Follow-up message at the next seq — must NOT re-trip any gap.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-6",
                "sender_user_id": "user-remote",
                "content": "next",
                "sender_seq": 6,
            },
        ),
    )
    assert await routing_repo.list_open_gaps("conv-1") == []
    assert (
        await routing_repo.peek_sender_seq(
            conversation_id="conv-1",
            sender_user_id="user-remote",
        )
        == 6
    )


async def test_dm_message_first_inbound_does_not_disturb_other_senders_gaps(db, bus):
    """The bogus-gap sweep is scoped to (conv, sender). Other senders'
    legitimately-recorded gap rows in the same conversation survive."""
    routing_repo = SqliteDmRoutingRepo(db)
    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        dm_routing_repo=routing_repo,
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("conv-1", "dm"),
    )
    # Two senders, both with rows in conversation_message_gaps.
    await routing_repo.insert_gaps(
        conversation_id="conv-1",
        sender_user_id="user-alice",
        expected_seqs=[1, 2],
    )
    await routing_repo.insert_gaps(
        conversation_id="conv-1",
        sender_user_id="user-bob",
        expected_seqs=[3],
    )

    # First inbound from alice triggers the sweep — only her rows go.
    await inbound._on_dm_message(
        _event(
            FederationEventType.DM_MESSAGE,
            {
                "conversation_id": "conv-1",
                "message_id": "m-3",
                "sender_user_id": "user-alice",
                "content": "hi",
                "sender_seq": 3,
            },
        ),
    )
    remaining = await routing_repo.list_open_gaps("conv-1")
    assert [(g["sender_user_id"], g["expected_seq"]) for g in remaining] == [
        ("user-bob", 3),
    ]


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
    # No public_relay in the payload ⇒ event carries None.
    assert captured[0].public_relay is None


_SEED_SPACE_SQL = """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                          identity_public_key, space_type, join_mode)
       VALUES(?,?,?,?,?,?,?)"""


def _seed_space_args(space_id):
    return (
        space_id,
        "Space",
        "peer-a",
        "owner",
        "aa" * 32,
        SpaceType.HOUSEHOLD.value,
        JoinMode.INVITE_ONLY.value,
    )


async def test_space_post_created_threads_public_relay(db, bus, inbound):
    """A public/global member broadcast carries the author household's
    pre-signed Phase-5a inner payload; inbound threads it verbatim onto
    the SpacePostCreated bus event (Phase 5a relay)."""
    await db.enqueue(_SEED_SPACE_SQL, _seed_space_args("sp-relay"))
    captured: list[SpacePostCreated] = []
    bus.subscribe(SpacePostCreated, captured.append)

    relay = {"signed": "inner-payload", "sig": "abc"}
    await inbound._on_space_post_created(
        _event(
            FederationEventType.SPACE_POST_CREATED,
            {
                "id": "post-relay",
                "author": "user-remote",
                "type": "text",
                "content": "hello",
                "public_relay": relay,
            },
            space_id="sp-relay",
        )
    )
    assert len(captured) == 1
    assert captured[0].public_relay == relay
    # public_relay must NOT leak into the persisted Post.
    assert not hasattr(captured[0].post, "public_relay")


async def test_space_post_created_no_public_relay_is_none(db, bus, inbound):
    await db.enqueue(_SEED_SPACE_SQL, _seed_space_args("sp-norelay"))
    captured: list[SpacePostCreated] = []
    bus.subscribe(SpacePostCreated, captured.append)

    await inbound._on_space_post_created(
        _event(
            FederationEventType.SPACE_POST_CREATED,
            {
                "id": "post-norelay",
                "author": "user-remote",
                "type": "text",
                "content": "hello",
            },
            space_id="sp-norelay",
        )
    )
    assert len(captured) == 1
    assert captured[0].public_relay is None


async def test_space_post_created_non_dict_public_relay_coerced_to_none(
    db, bus, inbound
):
    await db.enqueue(_SEED_SPACE_SQL, _seed_space_args("sp-badrelay"))
    captured: list[SpacePostCreated] = []
    bus.subscribe(SpacePostCreated, captured.append)

    await inbound._on_space_post_created(
        _event(
            FederationEventType.SPACE_POST_CREATED,
            {
                "id": "post-badrelay",
                "author": "user-remote",
                "type": "text",
                "content": "hello",
                "public_relay": "not-a-dict",
            },
            space_id="sp-badrelay",
        )
    )
    assert len(captured) == 1
    assert captured[0].public_relay is None


async def test_space_post_created_carries_location(db, bus, inbound):
    """Location posts ride on SPACE_POST_CREATED with a `location` block
    in the payload. Inbound decodes it into a LocationData."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-loc",
            "Space",
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
                "id": "post-loc",
                "author": "user-remote",
                "type": "location",
                "content": "Sunset",
                "location": {"lat": 52.5200, "lon": 4.0600, "label": "Marina"},
            },
            space_id="sp-loc",
        )
    )
    assert captured and captured[0].post.location is not None
    loc = captured[0].post.location
    assert loc.lat == 52.5200
    assert loc.lon == 4.0600
    assert loc.label == "Marina"


async def test_space_post_created_truncates_full_precision_gps(db, bus, inbound):
    """§25 / CLAUDE.md GPS rule: a peer can put full precision on the
    wire, but the receiver MUST truncate to 4 decimal places before
    persisting / publishing — same invariant as outbound posts. The
    persisted Post.location lat/lon should match
    ``round(raw, 4)`` exactly."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-trunc",
            "Space",
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
                "id": "post-trunc",
                "author": "user-remote",
                "type": "location",
                "location": {
                    "lat": 52.523456789,
                    "lon": 4.067123456,
                    "label": "Park",
                },
            },
            space_id="sp-trunc",
        )
    )
    assert captured and captured[0].post.location is not None
    loc = captured[0].post.location
    assert loc.lat == 52.5235
    assert loc.lon == 4.0671
    # Defence in depth: re-rounding the persisted value MUST be a no-op
    # (i.e. no extra decimals snuck through).
    assert round(loc.lat, 4) == loc.lat
    assert round(loc.lon, 4) == loc.lon


async def test_space_post_created_drops_malformed_location(db, bus, inbound):
    """A peer that sends a non-numeric lat shouldn't break the post —
    the location is silently dropped, the rest is preserved."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-loc-bad",
            "Space",
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
                "id": "post-loc-bad",
                "author": "user-remote",
                "type": "location",
                "location": {"lat": "not-a-number", "lon": 4.0600},
            },
            space_id="sp-loc-bad",
        )
    )
    assert captured
    assert captured[0].post.location is None


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


async def _seed_role_space(db, *, owner="peer-a"):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "sp-role",
            "Space",
            owner,
            "owner",
            "aa" * 32,
            SpaceType.HOUSEHOLD.value,
            JoinMode.INVITE_ONLY.value,
        ),
    )
    await SqliteSpaceRepo(db).save_member(
        SpaceMember(
            space_id="sp-role",
            user_id="u-1",
            role="member",
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )


async def test_space_member_role_changed_promotes_local_member(db, bus, inbound):
    """A host promotion is applied to the local space_members role so admin
    guards + the SPA see it (regression: the event used to be unhandled)."""
    await _seed_role_space(db)
    captured: list = []

    async def _cap(e):
        captured.append(e)

    bus.subscribe(SpaceConfigChanged, _cap)
    await inbound._on_space_member_role_changed(
        _event(
            FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            {"user_id": "u-1", "role": "admin"},
            space_id="sp-role",
            from_instance="peer-a",  # the owner/host
        )
    )
    row = await db.fetchone(
        "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
        ("sp-role", "u-1"),
    )
    assert row["role"] == "admin"
    # Publishes a local config-changed so connected tabs refresh.
    assert len(captured) == 1


async def test_space_member_role_changed_rejects_non_host(db, inbound):
    """Roles are host-authoritative — a role change from anyone other than
    the owning instance is dropped (no privilege spoofing)."""
    await _seed_role_space(db, owner="peer-a")
    await inbound._on_space_member_role_changed(
        _event(
            FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            {"user_id": "u-1", "role": "admin"},
            space_id="sp-role",
            from_instance="peer-b",  # NOT the owner
        )
    )
    row = await db.fetchone(
        "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
        ("sp-role", "u-1"),
    )
    assert row["role"] == "member"  # unchanged


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
    # v_23: SPACE_MEMBER_JOINED / SPACE_MEMBER_LEFT are intentionally NOT
    # registered here — the authority-verifying gossip handler in
    # PrivateSpaceInviteHandler is their sole handler (unsigned roster
    # mutations must never apply).
    assert FederationEventType.SPACE_MEMBER_JOINED not in registered_types
    assert FederationEventType.SPACE_MEMBER_LEFT not in registered_types
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


async def test_user_removed_cascades_to_moments_highlights_and_dms(db, bus):
    """USER_REMOVED purges every moment, highlight, and conversation
    the deprovisioned user is involved in. Matches the "hide = remove"
    semantic the SPA copy promises in ConnectionDetail."""
    from datetime import timedelta

    from socialhome.domain.conversation import (
        Conversation,
        ConversationMessage,
        ConversationType,
    )
    from socialhome.domain.highlight import Highlight, HighlightFrame
    from socialhome.domain.moment import Moment
    from socialhome.repositories.highlight_repo import SqliteHighlightRepo
    from socialhome.repositories.moment_repo import SqliteMomentRepo

    moment_repo = SqliteMomentRepo(db)
    highlight_repo = SqliteHighlightRepo(db)
    conv_repo = SqliteConversationRepo(db)

    inbound = FederationInboundService(
        bus=bus,
        conversation_repo=conv_repo,
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        highlight_repo=highlight_repo,
        moment_repo=moment_repo,
    )

    hidden_uid = "u-hidden"
    other_uid = "u-other"
    now = datetime.now(timezone.utc)

    # Seed: a moment from the hidden user and one from someone else.
    await moment_repo.save(
        Moment(
            id="m-hidden",
            author_user_id=hidden_uid,
            content="hidden-author moment",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="peer-a",
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
            is_public=False,
            received_via="household",
        )
    )
    await moment_repo.save(
        Moment(
            id="m-other",
            author_user_id=other_uid,
            content="other moment",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            origin_instance_id="peer-a",
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
            is_public=False,
            received_via="household",
        )
    )

    # Seed: a highlight from the hidden user + one frame.
    from socialhome.domain.highlight import (
        HighlightAudience,
        HighlightFrameType,
    )

    await highlight_repo.save_highlight(
        Highlight(
            id="h-hidden",
            author_user_id=hidden_uid,
            highlight_date=now.date().isoformat(),
            audience_kind=HighlightAudience.ALL_PAIRED,
            audience=(),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
        )
    )
    await highlight_repo.save_frame(
        HighlightFrame(
            id="f-hidden",
            highlight_id="h-hidden",
            sequence=0,
            frame_type=HighlightFrameType.IMAGE,
            media_url="https://example.invalid/h.jpg",
            caption_text="hidden",
            caption_emoji=None,
            duration_ms=None,
            created_at=now.isoformat(),
        )
    )

    # Seed: a conversation with a message from the hidden user. Plus
    # a second conversation untouched by the hidden user to confirm
    # we don't over-purge.
    await conv_repo.create(
        Conversation(
            id="conv-touched",
            type=ConversationType.DM,
            created_at=now,
        )
    )
    await conv_repo.save_message(
        ConversationMessage(
            id="msg-1",
            conversation_id="conv-touched",
            sender_user_id=hidden_uid,
            content="hi from hidden",
            created_at=now,
        )
    )
    await conv_repo.create(
        Conversation(
            id="conv-clean",
            type=ConversationType.DM,
            created_at=now,
        )
    )
    await conv_repo.save_message(
        ConversationMessage(
            id="msg-2",
            conversation_id="conv-clean",
            sender_user_id=other_uid,
            content="hi from other",
            created_at=now,
        )
    )

    # Fire USER_REMOVED for the hidden user.
    await inbound._on_user_removed(
        _event(
            FederationEventType.USER_REMOVED,
            {"user_id": hidden_uid},
        )
    )

    # Hidden user's moment is gone; other user's moment survives.
    assert await moment_repo.get("m-hidden") is None
    assert await moment_repo.get("m-other") is not None

    # Hidden user's highlight is gone.
    assert await highlight_repo.get_highlight("h-hidden") is None

    # Conversation touched by the hidden user is hard-deleted; the
    # clean one survives.
    assert await conv_repo.get("conv-touched") is None
    assert await conv_repo.get("conv-clean") is not None


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
    from socialhome.repositories.report_repo import SqliteReportRepo
    from socialhome.services.report_service import ReportService

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
               remote_inbox_url, local_inbox_id,
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


async def test_users_sync_one_bad_user_does_not_drop_the_rest(
    db, inbound, caplog, monkeypatch
):
    """One failing user must not abort the tail of the envelope.

    The loop had no per-user guard, so a single raise (e.g. the UNIQUE
    ``(instance_id, remote_username)`` collision a peer's repaired user_id
    used to provoke — migration 0049) skipped every remaining user AND was
    swallowed by the dispatcher: silent, recurring data loss.
    """
    import logging

    await db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk,
               key_self_to_remote, key_remote_to_self,
               remote_inbox_url, local_inbox_id,
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
    real = type(inbound)._upsert_remote_user

    async def flaky(self, instance_id, payload):
        if payload.get("username") == "bob":
            raise RuntimeError("boom")
        await real(self, instance_id, payload)

    monkeypatch.setattr(type(inbound), "_upsert_remote_user", flaky)
    with caplog.at_level(logging.WARNING):
        await inbound._on_users_sync(
            _event(
                FederationEventType.USERS_SYNC,
                {
                    "users": [
                        {"user_id": "u-r1", "username": "alice"},
                        {"user_id": "u-r2", "username": "bob"},
                        {"user_id": "u-r3", "username": "carol"},
                    ]
                },
                from_instance="peer-a",
            )
        )

    rows = await db.fetchall("SELECT user_id FROM remote_users ORDER BY user_id")
    assert [r["user_id"] for r in rows] == ["u-r1", "u-r3"]
    assert any("boom" in r.getMessage() for r in caplog.records)


# ─── USERS_SYNC — per-user identity binding (proto v_25) ──────────


class _BindingFakeFederation:
    """Minimal federation stub exposing the sender's pinned identity key."""

    def __init__(self, keys: dict[str, bytes]):
        self._keys = keys
        self._event_registry = MagicMock()
        self.own_instance_id = "self"

    async def peer_identity_public_key(self, instance_id: str):
        return self._keys.get(instance_id)


async def _seed_peer(db, instance_id: str, identity_pk_hex: str) -> None:
    await db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk,
               key_self_to_remote, key_remote_to_self,
               remote_inbox_url, local_inbox_id,
               status, source, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            instance_id,
            "Peer",
            identity_pk_hex,
            "enc",
            "enc",
            "https://peer/wh",
            "wh-" + instance_id,
            "confirmed",
            "manual",
            "2026-01-01T00:00:00+00:00",
        ),
    )


def _binding_entry(
    *, instance_kp, user_kp, username="alice", display="Alice", identity_anchor=None
):
    """Build a USERS_SYNC per-user entry carrying the full self-verifying
    identity binding, exactly as the outbound helper emits it.

    When ``identity_anchor`` is supplied (a uuid-anchored user, proto v_26),
    ``user_id`` derives from the anchor (not the username), the anchor is
    baked into both signatures, and the entry carries the ``identity_anchor``
    wire field — matching the v_26 outbound shape.
    """
    from datetime import datetime, timezone

    from socialhome.crypto import (
        USER_SIG_SUITE_ED25519,
        build_user_identity_assertion,
        derive_instance_id,
        derive_user_id,
    )

    iid = derive_instance_id(instance_kp.public_key)
    derivation_input = identity_anchor if identity_anchor is not None else username
    uid = derive_user_id(instance_kp.public_key, derivation_input)
    assertion = build_user_identity_assertion(
        instance_seed=instance_kp.private_key,
        user_id=uid,
        instance_id=iid,
        username=username,
        display_name=display,
        issued_at=datetime.now(timezone.utc).isoformat(),
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        user_sig_suite=USER_SIG_SUITE_ED25519,
        identity_anchor=identity_anchor,
    )
    entry = {
        "user_id": uid,
        "username": username,
        "display_name": display,
        "user_identity_public_key": assertion.user_identity_public_key,
        "user_sig_suite": assertion.user_sig_suite,
        "user_signature": assertion.user_signature,
        "user_assertion_signature": assertion.signature,
        "user_assertion_issued_at": assertion.issued_at,
    }
    if identity_anchor is not None:
        entry["identity_anchor"] = identity_anchor
    return (iid, uid, entry)


async def test_users_sync_stores_verified_identity_binding(db, bus):
    from socialhome.crypto import generate_identity_keypair

    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid, uid, entry = _binding_entry(instance_kp=instance_kp, user_kp=user_kp)
    await _seed_peer(db, iid, instance_kp.public_key.hex())

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _BindingFakeFederation({iid: instance_kp.public_key})

    await service._on_users_sync(
        _event(FederationEventType.USERS_SYNC, {"users": [entry]}, from_instance=iid)
    )

    row = await db.fetchone(
        "SELECT user_identity_public_key FROM remote_users WHERE user_id=?",
        (uid,),
    )
    assert row["user_identity_public_key"] == user_kp.public_key.hex()


async def test_users_sync_stores_identity_anchor_for_anchored_user(db, bus):
    """A v_26 binding carrying ``identity_anchor`` (for a uuid-anchored user)
    verifies against the sender's pinned key and persists BOTH the pubkey and
    the anchor on ``remote_users``."""
    from socialhome.crypto import generate_identity_keypair

    anchor = "11111111-2222-3333-4444-555555555555"
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid, uid, entry = _binding_entry(
        instance_kp=instance_kp, user_kp=user_kp, identity_anchor=anchor
    )
    await _seed_peer(db, iid, instance_kp.public_key.hex())

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _BindingFakeFederation({iid: instance_kp.public_key})

    await service._on_users_sync(
        _event(FederationEventType.USERS_SYNC, {"users": [entry]}, from_instance=iid)
    )

    row = await db.fetchone(
        "SELECT user_identity_public_key, identity_anchor FROM remote_users "
        "WHERE user_id=?",
        (uid,),
    )
    assert row["user_identity_public_key"] == user_kp.public_key.hex()
    assert row["identity_anchor"] == anchor


async def test_users_sync_forged_anchor_not_stored_but_upserted(db, bus, caplog):
    """A forged anchor — one whose derivation doesn't match the asserted
    user_id — fails verify and is rejected fail-soft: no pubkey/anchor stored,
    but the legacy upsert still lands."""
    import logging

    from socialhome.crypto import generate_identity_keypair

    real_anchor = "11111111-2222-3333-4444-555555555555"
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid, uid, entry = _binding_entry(
        instance_kp=instance_kp, user_kp=user_kp, identity_anchor=real_anchor
    )
    await _seed_peer(db, iid, instance_kp.public_key.hex())
    # Tamper: swap in a DIFFERENT anchor. user_id still derives from the real
    # anchor, so the verifier's user_id == derive(pk, forged_anchor) check fails
    # (and the instance signature committed to the real anchor anyway).
    entry["identity_anchor"] = "99999999-8888-7777-6666-555555555555"

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _BindingFakeFederation({iid: instance_kp.public_key})

    with caplog.at_level(logging.WARNING):
        await service._on_users_sync(
            _event(
                FederationEventType.USERS_SYNC, {"users": [entry]}, from_instance=iid
            )
        )

    row = await db.fetchone(
        "SELECT user_identity_public_key, identity_anchor, display_name "
        "FROM remote_users WHERE user_id=?",
        (uid,),
    )
    # Legacy upsert still happened; the unverified key + anchor were NOT stored.
    assert row is not None
    assert row["display_name"] == "Alice"
    assert row["user_identity_public_key"] is None
    assert row["identity_anchor"] is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_users_sync_anchor_roundtrip_outbound_to_inbound(db, bus):
    """End-to-end: the v_26 outbound helper's exact entry shape (with anchor),
    fed to the inbound handler, lands a verified pubkey + anchor."""
    from socialhome.crypto import (
        derive_instance_id,
        derive_user_id,
        generate_identity_keypair,
    )
    from socialhome.services.user_identity_binding import (
        user_identity_binding_fields,
    )

    anchor = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid = derive_instance_id(instance_kp.public_key)
    uid = derive_user_id(instance_kp.public_key, anchor)
    await _seed_peer(db, iid, instance_kp.public_key.hex())

    class _OutFed:
        own_identity_seed = instance_kp.private_key
        own_instance_id = iid

        async def peer_supports(self, instance_id, *, min_version):
            return True  # v_26+ peer: supports both binding + anchor

    class _OutUserRepo:
        async def get_user_identity_keypair(self, username):
            return (user_kp.public_key, user_kp.private_key)

        async def get_user_identity_anchor(self, username):
            return anchor

    binding = await user_identity_binding_fields(
        federation_service=_OutFed(),
        user_repo=_OutUserRepo(),
        peer_instance_id="anybody",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )
    assert binding["identity_anchor"] == anchor
    entry = {
        "user_id": uid,
        "username": "alice",
        "display_name": "Alice",
        **binding,
    }

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _BindingFakeFederation({iid: instance_kp.public_key})

    await service._on_users_sync(
        _event(FederationEventType.USERS_SYNC, {"users": [entry]}, from_instance=iid)
    )

    row = await db.fetchone(
        "SELECT user_identity_public_key, identity_anchor FROM remote_users "
        "WHERE user_id=?",
        (uid,),
    )
    assert row["user_identity_public_key"] == user_kp.public_key.hex()
    assert row["identity_anchor"] == anchor


async def test_users_sync_tampered_user_signature_not_stored_but_upserted(
    db, bus, caplog
):
    import logging

    from socialhome.crypto import generate_identity_keypair

    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    other_kp = generate_identity_keypair()
    iid, uid, entry = _binding_entry(instance_kp=instance_kp, user_kp=user_kp)
    await _seed_peer(db, iid, instance_kp.public_key.hex())
    # Swap in a foreign user self-signature → the binding self-sig is invalid.
    _, _, other_entry = _binding_entry(instance_kp=instance_kp, user_kp=other_kp)
    entry["user_signature"] = other_entry["user_signature"]

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _BindingFakeFederation({iid: instance_kp.public_key})

    with caplog.at_level(logging.WARNING):
        await service._on_users_sync(
            _event(
                FederationEventType.USERS_SYNC, {"users": [entry]}, from_instance=iid
            )
        )

    row = await db.fetchone(
        "SELECT user_identity_public_key, display_name FROM remote_users "
        "WHERE user_id=?",
        (uid,),
    )
    # Legacy upsert still happened; the unverified key was NOT stored.
    assert row is not None
    assert row["display_name"] == "Alice"
    assert row["user_identity_public_key"] is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_users_sync_unknown_sig_suite_not_stored(db, bus):
    from socialhome.crypto import generate_identity_keypair

    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid, uid, entry = _binding_entry(instance_kp=instance_kp, user_kp=user_kp)
    await _seed_peer(db, iid, instance_kp.public_key.hex())
    entry["user_sig_suite"] = "ed25519+martian"

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _BindingFakeFederation({iid: instance_kp.public_key})

    await service._on_users_sync(
        _event(FederationEventType.USERS_SYNC, {"users": [entry]}, from_instance=iid)
    )
    row = await db.fetchone(
        "SELECT user_identity_public_key FROM remote_users WHERE user_id=?",
        (uid,),
    )
    assert row is not None
    assert row["user_identity_public_key"] is None


async def test_users_sync_instance_id_mismatch_not_stored(db, bus):
    """A peer asserting a binding whose instance_id is NOT the sender it's
    attributed to must not get its key stored (fail-soft)."""
    from socialhome.crypto import generate_identity_keypair

    instance_kp = generate_identity_keypair()  # the real issuer in the assertion
    sender_kp = generate_identity_keypair()  # the envelope sender (different)
    user_kp = generate_identity_keypair()
    _iss_iid, uid, entry = _binding_entry(instance_kp=instance_kp, user_kp=user_kp)

    from socialhome.crypto import derive_instance_id

    sender_iid = derive_instance_id(sender_kp.public_key)
    await _seed_peer(db, sender_iid, sender_kp.public_key.hex())

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _BindingFakeFederation(
        {sender_iid: sender_kp.public_key}
    )

    # Attribute the entry to the SENDER, not the issuer baked into the assertion.
    await service._on_users_sync(
        _event(
            FederationEventType.USERS_SYNC,
            {"users": [entry]},
            from_instance=sender_iid,
        )
    )
    row = await db.fetchone(
        "SELECT user_identity_public_key FROM remote_users WHERE user_id=?",
        (uid,),
    )
    # The row may not even exist (user_id is derived from the issuer key, not
    # the sender), but if it does the key must not be stored.
    if row is not None:
        assert row["user_identity_public_key"] is None


async def test_users_sync_absent_binding_stores_no_key(db, bus):
    iid = "peer-legacy"
    await _seed_peer(db, iid, "ab" * 32)
    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _BindingFakeFederation({})

    await service._on_users_sync(
        _event(
            FederationEventType.USERS_SYNC,
            {"users": [{"user_id": "u-leg", "username": "leg", "display_name": "Leg"}]},
            from_instance=iid,
        )
    )
    row = await db.fetchone(
        "SELECT user_identity_public_key FROM remote_users WHERE user_id=?",
        ("u-leg",),
    )
    assert row is not None
    assert row["user_identity_public_key"] is None


async def test_users_sync_binding_skipped_when_federation_unattached(db, bus):
    """A binding-bearing entry on a service with no federation handle attached
    upserts the legacy row but stores no key (fail-soft, no raise)."""
    from socialhome.crypto import generate_identity_keypair

    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid, uid, entry = _binding_entry(instance_kp=instance_kp, user_kp=user_kp)
    await _seed_peer(db, iid, instance_kp.public_key.hex())

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    # No _federation_service attached.

    await service._on_users_sync(
        _event(FederationEventType.USERS_SYNC, {"users": [entry]}, from_instance=iid)
    )
    row = await db.fetchone(
        "SELECT user_identity_public_key FROM remote_users WHERE user_id=?",
        (uid,),
    )
    assert row is not None
    assert row["user_identity_public_key"] is None


async def test_user_updated_stores_handle(db, bus):
    """A ``USER_UPDATED`` payload carrying ``handle='bobby'`` lands the handle
    on the user's ``remote_users`` row (single row, keyed by user_id)."""
    await _seed_peer(db, "peer-a", "ab" * 32)
    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    await service._on_user_updated(
        _event(
            FederationEventType.USER_UPDATED,
            {
                "user_id": "u-h1",
                "username": "bob",
                "display_name": "Bob",
                "handle": "bobby",
            },
            from_instance="peer-a",
        )
    )
    rows = await db.fetchall(
        "SELECT user_id, handle FROM remote_users WHERE user_id=?",
        ("u-h1",),
    )
    assert len(rows) == 1
    assert rows[0]["handle"] == "bobby"


async def test_user_updated_without_handle_does_not_null_existing(db, bus):
    """An older-peer ``USER_UPDATED`` (no ``handle`` field) must not overwrite a
    previously-stored handle to NULL — the handle is sticky display metadata."""
    await _seed_peer(db, "peer-a", "ab" * 32)
    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    # First a handle-bearing update lands the handle.
    await service._on_user_updated(
        _event(
            FederationEventType.USER_UPDATED,
            {
                "user_id": "u-h2",
                "username": "bob",
                "display_name": "Bob",
                "handle": "bobby",
            },
            from_instance="peer-a",
        )
    )
    # Then an older-peer update with NO handle field arrives (e.g. a bio edit).
    await service._on_user_updated(
        _event(
            FederationEventType.USER_UPDATED,
            {
                "user_id": "u-h2",
                "username": "bob",
                "display_name": "Bob Smith",
                "bio": "updated",
            },
            from_instance="peer-a",
        )
    )
    row = await db.fetchone(
        "SELECT handle, display_name FROM remote_users WHERE user_id=?",
        ("u-h2",),
    )
    assert row["handle"] == "bobby"  # untouched, not nulled
    assert row["display_name"] == "Bob Smith"  # other fields still update


async def test_users_sync_binding_skipped_when_sender_key_unknown(db, bus, caplog):
    """A binding from a sender whose pinned key can't be resolved is rejected
    fail-soft with a WARNING; the legacy row is still upserted."""
    import logging

    from socialhome.crypto import generate_identity_keypair

    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid, uid, entry = _binding_entry(instance_kp=instance_kp, user_kp=user_kp)
    await _seed_peer(db, iid, instance_kp.public_key.hex())

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    # Federation attached but it returns None for the sender's key.
    service._federation_service = _BindingFakeFederation({})

    with caplog.at_level(logging.WARNING):
        await service._on_users_sync(
            _event(
                FederationEventType.USERS_SYNC, {"users": [entry]}, from_instance=iid
            )
        )
    row = await db.fetchone(
        "SELECT user_identity_public_key, display_name FROM remote_users "
        "WHERE user_id=?",
        (uid,),
    )
    assert row is not None
    assert row["display_name"] == "Alice"
    assert row["user_identity_public_key"] is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_users_sync_binding_roundtrip_outbound_to_inbound(db, bus):
    """End-to-end: the outbound helper's exact entry shape, fed to the inbound
    handler, lands a verified user identity key on ``remote_users``."""
    from socialhome.crypto import (
        derive_instance_id,
        derive_user_id,
        generate_identity_keypair,
    )
    from socialhome.services.user_identity_binding import (
        user_identity_binding_fields,
    )

    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid = derive_instance_id(instance_kp.public_key)
    uid = derive_user_id(instance_kp.public_key, "alice")
    await _seed_peer(db, iid, instance_kp.public_key.hex())

    class _OutFed:
        own_identity_seed = instance_kp.private_key
        own_instance_id = iid

        async def peer_supports(self, instance_id, *, min_version):
            return True

    class _OutUserRepo:
        async def get_user_identity_keypair(self, username):
            return (user_kp.public_key, user_kp.private_key)

    binding = await user_identity_binding_fields(
        federation_service=_OutFed(),
        user_repo=_OutUserRepo(),
        peer_instance_id="anybody",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )
    entry = {
        "user_id": uid,
        "username": "alice",
        "display_name": "Alice",
        **binding,
    }

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _BindingFakeFederation({iid: instance_kp.public_key})

    await service._on_users_sync(
        _event(FederationEventType.USERS_SYNC, {"users": [entry]}, from_instance=iid)
    )
    row = await db.fetchone(
        "SELECT user_identity_public_key FROM remote_users WHERE user_id=?",
        (uid,),
    )
    assert row["user_identity_public_key"] == user_kp.public_key.hex()


# ─── SPACE_MEDIA_BLOB — write the bytes the sender shipped ─────────


async def test_space_media_blob_writes_bytes_to_local_media_dir(
    db,
    bus,
    tmp_path,
):
    """The inbound receiver writes the bytes under the SAME filename
    the sender used so the relative ``<img src>`` on the post row
    resolves on this household. Without this handler the post lands
    but the picture is broken."""
    import base64

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    body = b"WEBP-bytes-from-the-sender"
    await service._on_space_media_blob(
        _event(
            FederationEventType.SPACE_MEDIA_BLOB,
            {
                "post_id": "post-mediated",
                "space_id": "sp-mediated",
                "filename": "abc123.webp",
                "mime_type": "image/webp",
                "bytes_base64": base64.b64encode(body).decode("ascii"),
            },
        )
    )
    assert (tmp_path / "abc123.webp").read_bytes() == body


async def test_space_media_blob_prefers_raw_media_bytes(db, bus, tmp_path):
    """Binary ``fed-media-v1`` path: ``event.media_bytes`` is used and
    written verbatim, with NO base64 field in the payload."""
    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    body = b"\x00\x01raw-binary-no-base64\xff"
    await service._on_space_media_blob(
        _event(
            FederationEventType.SPACE_MEDIA_BLOB,
            {
                "post_id": "post-bin",
                "space_id": "sp-bin",
                "filename": "binary.webp",
                "transfer_id": "tx-1",
                "chunk_count": 1,
                "final": True,
            },
            media_bytes=body,
        )
    )
    assert (tmp_path / "binary.webp").read_bytes() == body


async def test_dm_media_blob_prefers_raw_media_bytes(db, bus, tmp_path):
    """DM binary path: ``event.media_bytes`` lands as the final file and
    flips ``media_sync_status`` even with no ``bytes_b64`` present."""
    convo_repo = SqliteConversationRepo(db)
    service = FederationInboundService(
        bus=bus,
        conversation_repo=convo_repo,
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    body = b"\x89PNG raw dm media bytes"
    await service._on_dm_media_blob(
        _event(
            FederationEventType.DM_MEDIA_BLOB,
            {
                "media_blob_id": "mblob-1",
                "message_id": "mblob-1",
                "conversation_id": "c-1",
                "mime_type": "image/png",
                "chunk_index": 0,
                "chunk_count": 1,
                "final": True,
            },
            media_bytes=body,
        )
    )
    # The single-chunk fast path writes ``<message_id><ext>``.
    written = tmp_path / "mblob-1.png"
    assert written.read_bytes() == body


async def test_space_media_blob_rejects_path_traversal(db, bus, tmp_path):
    """A malicious sender could ship ``filename: '../../etc/passwd'``;
    same allowlist as ``MediaServeView`` rejects ``/``, ``\\``, or
    leading ``.``."""
    import base64

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    body = b"injection-attempt"
    for bad in ("../escape.webp", "sub/dir.webp", ".hidden.webp"):
        await service._on_space_media_blob(
            _event(
                FederationEventType.SPACE_MEDIA_BLOB,
                {
                    "post_id": "p",
                    "filename": bad,
                    "bytes_base64": base64.b64encode(body).decode("ascii"),
                },
            )
        )
    # Nothing was written outside the media dir.
    contents = list(tmp_path.iterdir())
    assert contents == [] or all(
        c.name not in ("escape.webp", "dir.webp", "hidden.webp") for c in contents
    )


async def test_space_media_blob_no_media_dir_drops(db, bus):
    """Receiver without ``media_dir`` (test stack) silently no-ops
    rather than crashing."""
    import base64

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    # Should not raise.
    await service._on_space_media_blob(
        _event(
            FederationEventType.SPACE_MEDIA_BLOB,
            {
                "filename": "x.webp",
                "bytes_base64": base64.b64encode(b"x").decode("ascii"),
            },
        )
    )


async def test_space_media_blob_assembles_chunked_transfer(db, bus, tmp_path):
    """Multi-chunk transfer: each chunk lands in a per-transfer
    partial dir; the final chunk triggers concat into the target
    filename. Mirrors :class:`SpaceMediaSyncService`'s sender shape."""
    import base64

    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
        media_dir=tmp_path,
    )
    full = b"".join(bytes([i]) * 256 for i in range(8))  # 2 KiB
    # Split into 4 chunks of 512 bytes each.
    chunks = [full[i * 512 : (i + 1) * 512] for i in range(4)]
    for idx, chunk in enumerate(chunks):
        await service._on_space_media_blob(
            _event(
                FederationEventType.SPACE_MEDIA_BLOB,
                {
                    "post_id": "p",
                    "transfer_id": "tx-1",
                    "filename": "big.webm",
                    "chunk_index": idx,
                    "chunk_count": 4,
                    "final": idx == 3,
                    "bytes_b64": base64.b64encode(chunk).decode("ascii"),
                },
            )
        )
    assert (tmp_path / "big.webm").read_bytes() == full
    # Partial directory cleaned up.
    assert not (tmp_path / ".partial" / "tx-1").exists()


# ─── Space config sequence gate (PR follow-up to #459) ────────────────


async def _seed_space(db, *, space_id="sp-cfg", from_instance="peer-a", seq=0):
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode,
                              config_sequence)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            space_id,
            "Cfg Space",
            from_instance,
            "owner",
            "aa" * 32,
            SpaceType.PRIVATE.value,
            JoinMode.INVITE_ONLY.value,
            seq,
        ),
    )


def _cfg_event(*, space_id, from_instance, sequence, name):
    """SPACE_CONFIG_CHANGED envelope shaped like SpaceConfigOutbound (#459).

    Carries both the flat legacy fields AND ``space_meta`` (modern shape)
    plus a top-level ``sequence`` — matches the on-wire shape the
    receiver guard reads.
    """
    return _event(
        FederationEventType.SPACE_CONFIG_CHANGED,
        {
            "space_id": space_id,
            "sequence": sequence,
            "event_type": "rename",
            "name": name,
            "join_mode": "invite_only",
            "space_type": "private",
            "features": {
                "calendar": True,
                "todo": True,
                "location": False,
                "location_mode": "gps",
                "stickies": True,
                "pages": True,
                "gallery": True,
            },
            "space_meta": {
                "name": name,
                "owner_instance_id": from_instance,
                "owner_username": "owner",
                "identity_public_key": "aa" * 32,
                "config_sequence": sequence,
                "space_type": "private",
                "join_mode": "invite_only",
                "features": {
                    "calendar": True,
                    "todo": True,
                    "location": False,
                    "location_mode": "gps",
                    "stickies": True,
                    "pages": True,
                    "gallery": True,
                },
            },
        },
        from_instance=from_instance,
        space_id=space_id,
    )


async def test_space_config_changed_applies_when_sequence_advances(
    db,
    bus,
    inbound,
):
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-a",
            sequence=6,
            name="Renamed",
        ),
    )
    row = await db.fetchone(
        "SELECT name, config_sequence FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "Renamed"
    assert row["config_sequence"] == 6


async def test_space_config_changed_dropped_on_stale_sequence(
    db,
    bus,
    inbound,
):
    """Out-of-order delivery: an older snapshot must not clobber a newer
    one. The receiver guards on ``incoming.sequence > existing``."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=10)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-a",
            sequence=7,  # stale
            name="StaleName",
        ),
    )
    row = await db.fetchone(
        "SELECT name, config_sequence FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    # No clobber.
    assert row["name"] == "Cfg Space"
    assert row["config_sequence"] == 10


async def test_space_config_changed_dropped_on_equal_sequence(
    db,
    bus,
    inbound,
):
    """Equal sequence = same state, no-op. Don't waste an UPSERT."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=4)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-a",
            sequence=4,
            name="ShouldNotApply",
        ),
    )
    row = await db.fetchone(
        "SELECT name FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "Cfg Space"


async def test_space_config_changed_missing_sequence_falls_through(
    db,
    bus,
    inbound,
):
    """Older sender doesn't ship ``sequence`` — the guard treats this
    as "no sequence available" and applies the upsert anyway. Otherwise
    a sender's omission would silently lock the receiver out of legit
    updates.
    """
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=2)
    event = _cfg_event(
        space_id="sp-cfg",
        from_instance="peer-a",
        sequence=99,  # value here ignored — we strip the field below
        name="ShouldApply",
    )
    del event.payload["sequence"]
    del event.payload["space_meta"]["config_sequence"]
    await inbound._on_space_config_changed(event)
    row = await db.fetchone(
        "SELECT name FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "ShouldApply"


async def test_space_config_changed_wrong_owner_drops(db, bus, inbound):
    """Defence-in-depth: only the row's owner_instance_id can update
    via this path. A second peer can't spoof a config change for
    somebody else's space."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-b",  # not the owner
            sequence=10,
            name="Spoofed",
        ),
    )
    row = await db.fetchone(
        "SELECT name FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "Cfg Space"


async def test_space_config_changed_ignored_after_remote_dissolve(db, bus, inbound):
    """Terminal state: once the host dissolved this space (locally archived
    with ``archived_reason='dissolved'``), a LATER higher-sequence
    SPACE_CONFIG_CHANGED with ``archived=False`` must NOT revive it. The
    snapshot is ignored — the space stays archived with its dissolve reason
    intact."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    # Host dissolved → member archived its local copy read-only.
    await db.enqueue(
        "UPDATE spaces SET archived=1, archived_reason='dissolved' WHERE id=?",
        ("sp-cfg",),
    )
    event = _cfg_event(
        space_id="sp-cfg",
        from_instance="peer-a",
        sequence=9,  # higher than existing 5
        name="Revived",
    )
    # A revival attempt would carry archived=False in the snapshot.
    event.payload["space_meta"]["archived"] = False
    await inbound._on_space_config_changed(event)
    row = await db.fetchone(
        "SELECT name, archived, archived_reason FROM spaces WHERE id=?",
        ("sp-cfg",),
    )
    assert row["name"] == "Cfg Space"  # snapshot ignored, no rename
    assert row["archived"] == 1
    assert row["archived_reason"] == "dissolved"


# ─── v_24: authority-signed config from a non-owner delegated admin ──────


async def _seed_signed_space(db, *, space_id, owner_instance, space_pub_hex, seq=0):
    """Seed a stub whose ``identity_public_key`` is a REAL Ed25519 pub so an
    authority signature actually verifies."""
    await db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
                              identity_public_key, space_type, join_mode,
                              config_sequence)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            space_id,
            "Cfg Space",
            owner_instance,
            "owner",
            space_pub_hex,
            SpaceType.PRIVATE.value,
            JoinMode.INVITE_ONLY.value,
            seq,
        ),
    )


def _signed_cfg_event(
    *,
    space_id,
    from_instance,
    owner_instance,
    sequence,
    name,
    seed,
    author=None,
    config_hlc=None,
    tamper=False,
    bad_sig=False,
    features=None,
):
    """Build a SPACE_CONFIG_CHANGED whose ``space_meta`` is authority-signed
    with the space seed. ``author`` records the config_author_instance carried
    in the (signed) meta; defaults to ``from_instance``. ``config_hlc`` (when
    given) stamps the migration-0037 LWW tie-break clock into the signed meta."""
    from socialhome.services.space_crypto_service import (
        sign_authority_event,
        strip_authority_sig_fields,
    )

    author = author if author is not None else from_instance
    meta = {
        "name": name,
        "owner_instance_id": owner_instance,
        "owner_username": "owner",
        "identity_public_key": "ignored-by-stub",
        "config_sequence": sequence,
        "config_author_instance": author,
        "space_type": "private",
        "join_mode": "invite_only",
        "features": features
        if features is not None
        else {
            "calendar": True,
            "todo": True,
            "location": False,
            "location_mode": "gps",
            "stickies": True,
            "pages": True,
            "gallery": True,
        },
    }
    if config_hlc is not None:
        meta["config_hlc"] = config_hlc
    signed = sign_authority_event(
        event_type="space_config_changed",
        space_id=space_id,
        payload=strip_authority_sig_fields(meta),
        space_seed=seed,
    )
    meta.update(signed)
    if tamper:
        # Mutate a signed field AFTER signing → signature no longer matches.
        meta["name"] = name + " (tampered)"
    if bad_sig:
        meta["authority_sig"] = "AAAA" + meta["authority_sig"][4:]
    return _event(
        FederationEventType.SPACE_CONFIG_CHANGED,
        {
            "space_id": space_id,
            "sequence": sequence,
            "event_type": "rename",
            "space_meta": meta,
        },
        from_instance=from_instance,
        space_id=space_id,
    )


async def test_config_from_nonowner_authority_signed_applies(db, bus, inbound):
    """A delegated admin (NOT the owner) signs a config change with the space
    seed; the receiver accepts it by verifying against the space pubkey, NOT by
    checking from_instance == owner."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-auth",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-auth",
            from_instance="admin-i",  # a delegated admin household, not owner
            owner_instance="owner-i",
            sequence=6,
            name="RenamedByAdmin",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone(
        "SELECT name, config_sequence, config_author_instance FROM spaces WHERE id=?",
        ("sp-auth",),
    )
    assert row["name"] == "RenamedByAdmin"
    assert row["config_sequence"] == 6
    assert row["config_author_instance"] == "admin-i"


def _own_instance(inbound, instance_id):
    """Give the inbound service an ``own_instance_id`` the handlers can read.

    Mirrors what ``attach_to`` stashes at runtime (``_federation_service``),
    without standing up a whole FederationService."""

    inbound._federation_service = SimpleNamespace(own_instance_id=instance_id)


_OWNER_ONLY_FEATURES_ON = {
    "calendar": True,
    "todo": True,
    "location": True,
    "location_mode": "gps",
    "stickies": True,
    "pages": True,
    "gallery": True,
    "allow_subscribers": True,
    "delegated_admin_authority": True,
}


@pytest.mark.security
async def test_config_changed_cannot_flip_owner_only_flags_on_the_host(
    db, bus, inbound, caplog
):
    """F2 SECURITY: the authority-signed v_24 path accepts a whole ``features``
    block from ANY seed holder. On the household that HOSTS the space that
    would let a delegated admin expose the space publicly (``allow_subscribers``)
    or grant itself delegation — writing over the owner's own row. Both
    owner-only flags stay pinned to the stored values; every other field in the
    block still applies."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-host",
        owner_instance="own-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    _own_instance(inbound, "own-i")  # we HOST this space
    with caplog.at_level(logging.INFO):
        await inbound._on_space_config_changed(
            _signed_cfg_event(
                space_id="sp-host",
                from_instance="admin-i",  # a seed-holding delegated admin
                owner_instance="own-i",
                sequence=9,
                name="RenamedByAdmin",
                seed=kp.private_key,
                features=_OWNER_ONLY_FEATURES_ON,
            )
        )
    space = await SqliteSpaceRepo(db).get("sp-host")
    assert space.features.allow_subscribers is False
    assert space.features.delegated_admin_authority is False
    # …the rest of the signed block still applied.
    assert space.name == "RenamedByAdmin"
    assert space.features.location is True
    assert space.config_sequence == 9
    assert "owner-only" in caplog.text


async def test_config_changed_applies_owner_only_flags_on_a_member_household(
    db, bus, inbound
):
    """F2 counterpart: a household that merely MIRRORS the space is not the
    authority for it — the owner's (or an authorised seed holder's) value for
    the owner-only flags is exactly what it should mirror. Pin only where we
    host."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-mirror",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    _own_instance(inbound, "own-i")  # we are NOT the host
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-mirror",
            from_instance="owner-i",
            owner_instance="owner-i",
            sequence=9,
            name="OwnerOpenedIt",
            seed=kp.private_key,
            features=_OWNER_ONLY_FEATURES_ON,
        )
    )
    space = await SqliteSpaceRepo(db).get("sp-mirror")
    assert space.features.allow_subscribers is True
    assert space.features.delegated_admin_authority is True
    assert space.name == "OwnerOpenedIt"


@pytest.mark.security
async def test_config_from_nonowner_tampered_sig_drops(db, bus, inbound):
    """SECURITY: a non-owner config whose signed payload was mutated after
    signing must be DROPPED — the signature no longer verifies."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-auth",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-auth",
            from_instance="admin-i",
            owner_instance="owner-i",
            sequence=6,
            name="Evil",
            seed=kp.private_key,
            tamper=True,
        )
    )
    row = await db.fetchone(
        "SELECT name, config_sequence FROM spaces WHERE id=?",
        ("sp-auth",),
    )
    assert row["name"] == "Cfg Space"  # dropped, no apply
    assert row["config_sequence"] == 5


@pytest.mark.security
async def test_config_from_nonowner_bad_sig_drops(db, bus, inbound):
    """SECURITY: a non-owner config with a corrupt authority_sig is DROPPED —
    a present-but-invalid sig never falls through to the owner gate."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-auth",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-auth",
            from_instance="admin-i",
            owner_instance="owner-i",
            sequence=6,
            name="Evil",
            seed=kp.private_key,
            bad_sig=True,
        )
    )
    row = await db.fetchone(
        "SELECT name FROM spaces WHERE id=?",
        ("sp-auth",),
    )
    assert row["name"] == "Cfg Space"


async def test_config_from_nonowner_no_sig_drops(db, bus, inbound):
    """A non-owner config with NO authority signature is dropped (today's
    owner-only behaviour) — only a signature relaxes the gate."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-b",  # not owner, no sig
            sequence=10,
            name="Spoofed",
        )
    )
    row = await db.fetchone("SELECT name FROM spaces WHERE id=?", ("sp-cfg",))
    assert row["name"] == "Cfg Space"


async def test_config_from_owner_without_sig_still_applies(db, bus, inbound):
    """Back-compat: an owner-originated config with no authority signature
    still applies through the legacy from_instance == owner path."""
    await _seed_space(db, space_id="sp-cfg", from_instance="peer-a", seq=5)
    await inbound._on_space_config_changed(
        _cfg_event(
            space_id="sp-cfg",
            from_instance="peer-a",  # the owner
            sequence=6,
            name="OwnerRename",
        )
    )
    row = await db.fetchone("SELECT name FROM spaces WHERE id=?", ("sp-cfg",))
    assert row["name"] == "OwnerRename"


async def test_config_lww_same_sequence_deterministic_tiebreak(db, bus, inbound):
    """Two concurrent same-sequence edits from different admins converge to the
    SAME winner regardless of arrival order — the higher author id wins; a
    lower-(seq,author) is dropped. Run both orderings and assert identical
    final state."""
    from socialhome.crypto import generate_space_keypair

    async def _converge(first_author, second_author):
        kp = generate_space_keypair()
        sid = f"sp-lww-{first_author}-{second_author}"
        await _seed_signed_space(
            db,
            space_id=sid,
            owner_instance="owner-i",
            space_pub_hex=kp.public_key.hex(),
            seq=5,
        )
        # Both edits bump from base 5 → seq 6, different authors.
        await inbound._on_space_config_changed(
            _signed_cfg_event(
                space_id=sid,
                from_instance=first_author,
                owner_instance="owner-i",
                sequence=6,
                name=f"By-{first_author}",
                author=first_author,
                seed=kp.private_key,
            )
        )
        await inbound._on_space_config_changed(
            _signed_cfg_event(
                space_id=sid,
                from_instance=second_author,
                owner_instance="owner-i",
                sequence=6,
                name=f"By-{second_author}",
                author=second_author,
                seed=kp.private_key,
            )
        )
        row = await db.fetchone(
            "SELECT name, config_author_instance FROM spaces WHERE id=?",
            (sid,),
        )
        return row["name"], row["config_author_instance"]

    # Higher author id ("admin-z") must win in both arrival orders.
    order1 = await _converge("admin-a", "admin-z")
    order2 = await _converge("admin-z", "admin-a")
    assert order1 == order2 == ("By-admin-z", "admin-z")


async def test_config_lww_lower_author_at_equal_seq_dropped(db, bus, inbound):
    """A second edit at the same sequence but a LOWER author id is dropped."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-lww2",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-lww2",
            from_instance="admin-z",
            owner_instance="owner-i",
            sequence=6,
            name="ByZ",
            author="admin-z",
            seed=kp.private_key,
        )
    )
    # Lower author, same seq → must NOT clobber.
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-lww2",
            from_instance="admin-a",
            owner_instance="owner-i",
            sequence=6,
            name="ByA",
            author="admin-a",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone("SELECT name FROM spaces WHERE id=?", ("sp-lww2",))
    assert row["name"] == "ByZ"


async def test_config_lww_higher_hlc_wins_at_equal_sequence(db, bus, inbound):
    """Migration 0037 — at the SAME config_sequence the edit with the HIGHER
    config_hlc wins (later edit), REGARDLESS of author ordering: a LOWER-id
    author with a higher HLC beats a higher-id author with a lower HLC."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-hlc",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    # First edit: higher-id author, LOWER hlc.
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-hlc",
            from_instance="admin-z",
            owner_instance="owner-i",
            sequence=6,
            name="ByZ-early",
            author="admin-z",
            config_hlc="1000-0",
            seed=kp.private_key,
        )
    )
    # Second edit: LOWER-id author but a strictly HIGHER hlc → later edit wins
    # despite the author ordering that decided pre-0037.
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-hlc",
            from_instance="admin-a",
            owner_instance="owner-i",
            sequence=6,
            name="ByA-late",
            author="admin-a",
            config_hlc="2000-0",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone(
        "SELECT name, config_hlc, config_author_instance FROM spaces WHERE id=?",
        ("sp-hlc",),
    )
    assert row["name"] == "ByA-late"
    assert row["config_hlc"] == "2000-0"
    assert row["config_author_instance"] == "admin-a"


async def test_config_lww_equal_hlc_falls_back_to_author(db, bus, inbound):
    """With EQUAL config_sequence and EQUAL config_hlc the author tie-break
    decides (unchanged): the higher-id author wins."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-hlc-eq",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-hlc-eq",
            from_instance="admin-a",
            owner_instance="owner-i",
            sequence=6,
            name="ByA",
            author="admin-a",
            config_hlc="1000-0",
            seed=kp.private_key,
        )
    )
    # Same seq + same HLC, higher author → wins on the author tie-break.
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-hlc-eq",
            from_instance="admin-z",
            owner_instance="owner-i",
            sequence=6,
            name="ByZ",
            author="admin-z",
            config_hlc="1000-0",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone("SELECT name FROM spaces WHERE id=?", ("sp-hlc-eq",))
    assert row["name"] == "ByZ"


async def test_config_lww_legacy_zero_hlc_falls_back_to_author(db, bus, inbound):
    """Backward-compat: a legacy incoming with NO config_hlc (→ HLC(0,0)) vs an
    existing zero-HLC row falls back to author-only ordering — behaviour-
    identical to pre-0037. Lower author at equal seq is dropped; higher wins."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-legacy-hlc",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    # No config_hlc on either edit → both HLC(0,0); author decides.
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-legacy-hlc",
            from_instance="admin-z",
            owner_instance="owner-i",
            sequence=6,
            name="ByZ",
            author="admin-z",
            seed=kp.private_key,
        )
    )
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-legacy-hlc",
            from_instance="admin-a",
            owner_instance="owner-i",
            sequence=6,
            name="ByA",
            author="admin-a",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone("SELECT name FROM spaces WHERE id=?", ("sp-legacy-hlc",))
    assert row["name"] == "ByZ"  # higher author wins, HLC tie — unchanged


async def test_config_lww_hlc_deterministic_convergence(db, bus, inbound):
    """Two receivers given the same two concurrent edits (same seq, DIFFERENT
    HLCs) converge to the SAME winner regardless of arrival order — the higher
    HLC wins on both, even though the higher-HLC author sorts LOWER."""
    from socialhome.crypto import generate_space_keypair

    async def _converge(first, second):
        kp = generate_space_keypair()
        sid = f"sp-conv-{first}-{second}"
        await _seed_signed_space(
            db,
            space_id=sid,
            owner_instance="owner-i",
            space_pub_hex=kp.public_key.hex(),
            seq=5,
        )
        # admin-a (lower id) carries the HIGHER hlc; admin-z the lower.
        edits = {
            "a": dict(
                from_instance="admin-a",
                author="admin-a",
                name="ByA",
                config_hlc="2000-0",
            ),
            "z": dict(
                from_instance="admin-z",
                author="admin-z",
                name="ByZ",
                config_hlc="1000-0",
            ),
        }
        for which in (first, second):
            await inbound._on_space_config_changed(
                _signed_cfg_event(
                    space_id=sid,
                    owner_instance="owner-i",
                    sequence=6,
                    seed=kp.private_key,
                    **edits[which],
                )
            )
        row = await db.fetchone(
            "SELECT name, config_hlc FROM spaces WHERE id=?", (sid,)
        )
        return row["name"], row["config_hlc"]

    order1 = await _converge("a", "z")
    order2 = await _converge("z", "a")
    # Higher HLC ("2000-0", authored by the LOWER-id admin-a) wins both ways.
    assert order1 == order2 == ("ByA", "2000-0")


async def test_config_lww_hlc_high_then_low_drops_late_lower(db, bus, inbound):
    """Reversed-order determinism: at the SAME config_sequence, when the
    HIGH-hlc edit arrives FIRST and the LOW-hlc edit arrives SECOND, the late
    lower edit is DROPPED — convergence to the HIGH-hlc winner is identical to
    the low-then-high order (the existing convergence test)."""
    from socialhome.crypto import generate_space_keypair

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-hlc-htl",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    # HIGH hlc arrives FIRST (authored by the lower-id admin-a).
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-hlc-htl",
            from_instance="admin-a",
            owner_instance="owner-i",
            sequence=6,
            name="ByA-high",
            author="admin-a",
            config_hlc="2000-0",
            seed=kp.private_key,
        )
    )
    # LOW hlc arrives SECOND (higher-id admin-z) → must be dropped despite the
    # author ordering that decided pre-0037.
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-hlc-htl",
            from_instance="admin-z",
            owner_instance="owner-i",
            sequence=6,
            name="ByZ-low",
            author="admin-z",
            config_hlc="1000-0",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone(
        "SELECT name, config_hlc, config_author_instance FROM spaces WHERE id=?",
        ("sp-hlc-htl",),
    )
    assert row["name"] == "ByA-high"  # late lower edit dropped
    assert row["config_hlc"] == "2000-0"
    assert row["config_author_instance"] == "admin-a"


async def test_config_hlc_far_future_dropped_clock_abuse_guard(db, bus, inbound):
    """SECURITY: a seed-holder can SIGN a far-future config_hlc to win every
    equal-sequence race forever. The §24.11 envelope-timestamp check bounds
    only the ENVELOPE timestamp, not the signed config_hlc field. The drift
    guard drops an edit whose signed config_hlc outruns the event's OWN signed
    envelope timestamp by more than HLC_MAX_DRIFT_MS — even though its
    (seq, hlc, author) would otherwise win."""
    from socialhome.crypto import generate_space_keypair
    from socialhome.infrastructure.hlc import HLC_MAX_DRIFT_MS

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-hlc-future",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    # An honest edit lands first (within-bound HLC near real now).
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-hlc-future",
            from_instance="admin-a",
            owner_instance="owner-i",
            sequence=6,
            name="Honest",
            author="admin-a",
            config_hlc=f"{now_ms}-0",
            seed=kp.private_key,
        )
    )
    # The attacker signs a far-future config_hlc at a HIGHER sequence — its
    # (seq, hlc, author) would dominate every honest equal/lower edit forever.
    far_future = now_ms + HLC_MAX_DRIFT_MS + 10**6
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-hlc-future",
            from_instance="admin-z",
            owner_instance="owner-i",
            sequence=7,
            name="ClockAbuse",
            author="admin-z",
            config_hlc=f"{far_future}-0",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone(
        "SELECT name, config_hlc, config_sequence FROM spaces WHERE id=?",
        ("sp-hlc-future",),
    )
    # Far-future edit DROPPED — the honest edit's state is unchanged.
    assert row["name"] == "Honest"
    assert row["config_hlc"] == f"{now_ms}-0"
    assert row["config_sequence"] == 6


async def test_config_hlc_within_drift_bound_applies(db, bus, inbound):
    """Control for the clock-abuse guard: a config_hlc within the drift bound
    (physical_ms <= envelope_ms + HLC_MAX_DRIFT_MS) at a winning sequence still
    applies normally."""
    from socialhome.crypto import generate_space_keypair
    from socialhome.infrastructure.hlc import HLC_MAX_DRIFT_MS

    kp = generate_space_keypair()
    await _seed_signed_space(
        db,
        space_id="sp-hlc-bound",
        owner_instance="owner-i",
        space_pub_hex=kp.public_key.hex(),
        seq=5,
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Within the bound: a few seconds of skew, well under HLC_MAX_DRIFT_MS.
    within = now_ms + (HLC_MAX_DRIFT_MS // 2)
    await inbound._on_space_config_changed(
        _signed_cfg_event(
            space_id="sp-hlc-bound",
            from_instance="admin-a",
            owner_instance="owner-i",
            sequence=6,
            name="WithinBound",
            author="admin-a",
            config_hlc=f"{within}-0",
            seed=kp.private_key,
        )
    )
    row = await db.fetchone(
        "SELECT name, config_hlc, config_sequence FROM spaces WHERE id=?",
        ("sp-hlc-bound",),
    )
    assert row["name"] == "WithinBound"
    assert row["config_hlc"] == f"{within}-0"
    assert row["config_sequence"] == 6
