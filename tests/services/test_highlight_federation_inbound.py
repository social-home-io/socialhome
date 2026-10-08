"""Inbound highlight federation — handlers in :class:`FederationInboundService`.

Covers:
* ``HIGHLIGHT_CREATED`` persists a Highlight + first frame and republishes
  :class:`HighlightFrameAdded` so the realtime layer fans the WS frame.
* ``HIGHLIGHT_FRAME_APPENDED`` lazily creates the parent highlight if the
  ``HIGHLIGHT_CREATED`` envelope arrived out-of-order.
* ``HIGHLIGHT_FRAME_DELETED`` and ``HIGHLIGHT_DELETED`` flip the bus events.
* Authority mismatch (envelope ``from_instance`` ≠ author's home
  instance) is dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.errors import InvalidMediaRefError
from socialhome.domain.events import (
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameRemoved,
    HighlightFrameViewed,
    HighlightRemoved,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.highlight import HighlightFrameType
from socialhome.domain.user import RemoteUser
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.repositories.highlight_repo import SqliteHighlightRepo
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound_service import FederationInboundService
from socialhome.services.highlight_federation_outbound import (
    HighlightFederationOutbound,
)
from socialhome.services.highlight_service import HighlightService


@pytest.fixture
async def inbound(db, bus):
    user_repo = SqliteUserRepo(db)
    # Seed one local user (so the authority check has something to look up
    # for non-author lookups) and one remote user that maps to peer-a.
    # ``remote_users.instance_id`` FKs ``remote_instances`` so we seed the
    # paired peer first.
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name) VALUES(?,?,?)",
        ("uid-local", "local", "Local"),
    )
    await db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk,
               key_self_to_remote, key_remote_to_self,
               remote_inbox_url, local_inbox_id
           ) VALUES(?,?,?,?,?,?,?)""",
        ("peer-a", "Peer A", "00" * 32, "k1", "k2", "https://peer-a/wh", "wh-a"),
    )
    await user_repo.upsert_remote(
        RemoteUser(
            user_id="uid-remote",
            instance_id="peer-a",
            remote_username="alice",
            display_name="Alice",
        ),
    )
    return FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=user_repo,
        highlight_repo=SqliteHighlightRepo(db),
    )


def _event(event_type, payload, *, from_instance="peer-a"):
    return FederationEvent(
        msg_id="msg-" + event_type.value,
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


def _create_payload(**over):
    base = {
        "highlight_id": "s-fed-1",
        "frame_id": "f-fed-1",
        "author_user_id": "uid-remote",
        "highlight_date": "2026-05-05",
        "sequence": 1,
        "audience_kind": "all_paired",
        "audience": [],
        "frame_type": "image",
        "media_url": "/api/media/x.webp",
        "caption_text": None,
        "caption_emoji": None,
        "duration_ms": None,
        "expires_at": "2026-06-04T00:00:00Z",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(over)
    return base


async def _seed_local_highlight(inbound) -> None:
    """``s-fed-1`` / ``f-fed-1`` authored by our own ``uid-local``.

    Views and reactions are unicast to the author's home, so a reply is
    only ever about a highlight authored on this household.
    """
    payload = _create_payload(author_user_id="uid-local")
    highlight = inbound._highlight_from_payload(payload)
    await inbound._highlight_repo.save_highlight(highlight)
    await inbound._highlight_repo.save_frame(
        inbound._frame_from_payload(highlight.id, payload)
    )


# ─── HIGHLIGHT_CREATED ────────────────────────────────────────────────────────


async def test_highlight_created_persists_highlight_and_frame(db, bus, inbound):
    captured: list[HighlightFrameAdded] = []
    bus.subscribe(HighlightFrameAdded, captured.append)

    await inbound._on_highlight_created(
        _event(FederationEventType.HIGHLIGHT_CREATED, _create_payload()),
    )

    highlight = await inbound._highlight_repo.get_highlight("s-fed-1")
    assert highlight is not None
    assert highlight.author_user_id == "uid-remote"
    frames = await inbound._highlight_repo.list_frames("s-fed-1")
    assert len(frames) == 1 and frames[0].id == "f-fed-1"
    assert len(captured) == 1
    assert captured[0].is_first_frame is True
    assert captured[0].highlight_id == "s-fed-1"


async def test_highlight_created_authority_mismatch_dropped(db, bus, inbound):
    """Envelope claims author-home != envelope sender → drop."""
    captured: list[HighlightFrameAdded] = []
    bus.subscribe(HighlightFrameAdded, captured.append)

    # Author 'uid-remote' lives on peer-a; envelope arrives from peer-b → mismatch
    await inbound._on_highlight_created(
        _event(
            FederationEventType.HIGHLIGHT_CREATED,
            _create_payload(),
            from_instance="peer-b",
        ),
    )
    assert await inbound._highlight_repo.get_highlight("s-fed-1") is None
    assert captured == []


async def test_highlight_frame_appended_creates_parent_if_missing(db, bus, inbound):
    """Out-of-order delivery: FRAME_APPENDED arrives before CREATED."""
    captured: list[HighlightFrameAdded] = []
    bus.subscribe(HighlightFrameAdded, captured.append)

    await inbound._on_highlight_frame_appended(
        _event(
            FederationEventType.HIGHLIGHT_FRAME_APPENDED,
            _create_payload(frame_id="f-fed-2", sequence=2),
        ),
    )
    highlight = await inbound._highlight_repo.get_highlight("s-fed-1")
    assert highlight is not None
    frames = await inbound._highlight_repo.list_frames("s-fed-1")
    assert [f.id for f in frames] == ["f-fed-2"]
    assert len(captured) == 1 and captured[0].is_first_frame is False


async def test_highlight_frame_appended_to_existing_highlight(db, bus, inbound):
    # Land HIGHLIGHT_CREATED first
    await inbound._on_highlight_created(
        _event(FederationEventType.HIGHLIGHT_CREATED, _create_payload()),
    )
    captured: list[HighlightFrameAdded] = []
    bus.subscribe(HighlightFrameAdded, captured.append)

    await inbound._on_highlight_frame_appended(
        _event(
            FederationEventType.HIGHLIGHT_FRAME_APPENDED,
            _create_payload(frame_id="f-fed-2", sequence=2),
        ),
    )
    frames = await inbound._highlight_repo.list_frames("s-fed-1")
    assert {f.id for f in frames} == {"f-fed-1", "f-fed-2"}
    assert len(captured) == 1
    assert captured[0].frame_id == "f-fed-2"
    assert captured[0].is_first_frame is False


async def test_highlight_frame_deleted_removes_and_publishes(db, bus, inbound):
    await inbound._on_highlight_created(
        _event(FederationEventType.HIGHLIGHT_CREATED, _create_payload()),
    )
    captured: list[HighlightFrameRemoved] = []
    bus.subscribe(HighlightFrameRemoved, captured.append)

    await inbound._on_highlight_frame_deleted(
        _event(
            FederationEventType.HIGHLIGHT_FRAME_DELETED,
            {"highlight_id": "s-fed-1", "frame_id": "f-fed-1"},
        ),
    )
    assert await inbound._highlight_repo.get_frame("f-fed-1") is None
    assert len(captured) == 1


async def test_highlight_deleted_removes_highlight_and_publishes(db, bus, inbound):
    await inbound._on_highlight_created(
        _event(FederationEventType.HIGHLIGHT_CREATED, _create_payload()),
    )
    captured: list[HighlightRemoved] = []
    bus.subscribe(HighlightRemoved, captured.append)

    await inbound._on_highlight_deleted(
        _event(
            FederationEventType.HIGHLIGHT_DELETED,
            {"highlight_id": "s-fed-1", "author_user_id": "uid-remote"},
        ),
    )
    assert await inbound._highlight_repo.get_highlight("s-fed-1") is None
    assert len(captured) == 1


async def test_handlers_registered_when_highlight_repo_present(db, bus):
    """attach_to() registers the 4 HIGHLIGHT_* handlers when the repo is wired."""
    user_repo = SqliteUserRepo(db)
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=user_repo,
        highlight_repo=SqliteHighlightRepo(db),
    )
    fake_fed = type("F", (), {})()
    fake_fed._event_registry = type(
        "R",
        (),
        {
            "_handlers": {},
            "register": lambda self, t, h: self._handlers.__setitem__(t, h),
        },
    )()
    svc.attach_to(fake_fed)
    registered = set(fake_fed._event_registry._handlers.keys())
    assert FederationEventType.HIGHLIGHT_CREATED in registered
    assert FederationEventType.HIGHLIGHT_FRAME_APPENDED in registered
    assert FederationEventType.HIGHLIGHT_FRAME_DELETED in registered
    assert FederationEventType.HIGHLIGHT_DELETED in registered
    assert FederationEventType.HIGHLIGHT_FRAME_VIEWED in registered
    assert FederationEventType.HIGHLIGHT_FRAME_REACTED in registered
    assert FederationEventType.HIGHLIGHT_FRAME_REACTION_REMOVED in registered


async def test_highlight_frame_viewed_persists_and_publishes(db, bus, inbound):
    """A remote viewer's view receipt lands in highlight_frame_views and
    fires HighlightFrameViewed so realtime can ping the author."""
    # Seed the parent highlight + frame via the CREATED handler.
    await _seed_local_highlight(inbound)
    captured: list[HighlightFrameViewed] = []
    bus.subscribe(HighlightFrameViewed, captured.append)

    await inbound._on_highlight_frame_viewed(
        _event(
            FederationEventType.HIGHLIGHT_FRAME_VIEWED,
            {
                "highlight_id": "s-fed-1",
                "frame_id": "f-fed-1",
                # ``uid-remote`` lives on peer-a (seeded in the fixture);
                # so does the envelope from_instance — authority matches.
                "viewer_user_id": "uid-remote",
                "author_user_id": "uid-local",
            },
        ),
    )
    views = await inbound._highlight_repo.list_views_for_frame("f-fed-1")
    assert len(views) == 1 and views[0].viewer_user_id == "uid-remote"
    assert len(captured) == 1
    assert captured[0].viewer_user_id == "uid-remote"


async def test_highlight_frame_viewed_authority_mismatch_dropped(db, bus, inbound):
    """The viewer must live on the envelope's signed sender."""
    await inbound._on_highlight_created(
        _event(FederationEventType.HIGHLIGHT_CREATED, _create_payload()),
    )
    captured: list[HighlightFrameViewed] = []
    bus.subscribe(HighlightFrameViewed, captured.append)

    await inbound._on_highlight_frame_viewed(
        _event(
            FederationEventType.HIGHLIGHT_FRAME_VIEWED,
            {
                "highlight_id": "s-fed-1",
                "frame_id": "f-fed-1",
                "viewer_user_id": "uid-remote",  # lives on peer-a
                "author_user_id": "uid-local",
            },
            from_instance="peer-b",  # mismatch
        ),
    )
    assert await inbound._highlight_repo.list_views_for_frame("f-fed-1") == []
    assert captured == []


async def test_highlight_frame_reacted_persists_and_publishes(db, bus, inbound):
    await _seed_local_highlight(inbound)
    captured: list[HighlightFrameReactionChanged] = []
    bus.subscribe(HighlightFrameReactionChanged, captured.append)

    await inbound._on_highlight_frame_reacted(
        _event(
            FederationEventType.HIGHLIGHT_FRAME_REACTED,
            {
                "highlight_id": "s-fed-1",
                "frame_id": "f-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
                "emoji": "🔥",
            },
        ),
    )
    rs = await inbound._highlight_repo.list_reactions_for_frame("f-fed-1")
    assert len(rs) == 1 and rs[0].emoji == "🔥"
    assert len(captured) == 1 and captured[0].emoji == "🔥"


async def test_highlight_frame_reaction_removed_clears(db, bus, inbound):
    """REACTION_REMOVED clears the row and publishes ``emoji=None``."""
    await _seed_local_highlight(inbound)
    # Seed a reaction first.
    await inbound._on_highlight_frame_reacted(
        _event(
            FederationEventType.HIGHLIGHT_FRAME_REACTED,
            {
                "highlight_id": "s-fed-1",
                "frame_id": "f-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
                "emoji": "🔥",
            },
        ),
    )
    captured: list[HighlightFrameReactionChanged] = []
    bus.subscribe(HighlightFrameReactionChanged, captured.append)

    await inbound._on_highlight_frame_reaction_removed(
        _event(
            FederationEventType.HIGHLIGHT_FRAME_REACTION_REMOVED,
            {
                "highlight_id": "s-fed-1",
                "frame_id": "f-fed-1",
                "reactor_user_id": "uid-remote",
                "author_user_id": "uid-local",
            },
        ),
    )
    assert await inbound._highlight_repo.list_reactions_for_frame("f-fed-1") == []
    assert len(captured) == 1 and captured[0].emoji is None


async def test_handlers_skipped_when_highlight_repo_missing(db, bus):
    """Tests that don't pass a highlight_repo don't see HIGHLIGHT_* registered."""
    user_repo = SqliteUserRepo(db)
    svc = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=user_repo,
    )
    fake_fed = type("F", (), {})()
    fake_fed._event_registry = type(
        "R",
        (),
        {
            "_handlers": {},
            "register": lambda self, t, h: self._handlers.__setitem__(t, h),
        },
    )()
    svc.attach_to(fake_fed)
    registered = set(fake_fed._event_registry._handlers.keys())
    assert FederationEventType.HIGHLIGHT_CREATED not in registered


@pytest.mark.parametrize(
    "remote",
    ["https://tracker.example/pixel.png", "//tracker.example/p.png", "javascript:x"],
)
async def test_highlight_frame_with_a_remote_media_url_is_not_stored(
    db, bus, inbound, remote
):
    """F7: a frame whose ``media_url`` is not a local media reference is
    dropped — it would be an ``<img>`` / ``<video>`` src on a third party."""
    await inbound._on_highlight_created(
        _event(
            FederationEventType.HIGHLIGHT_CREATED, _create_payload(media_url=remote)
        ),
    )
    await inbound._on_highlight_frame_appended(
        _event(
            FederationEventType.HIGHLIGHT_FRAME_APPENDED,
            _create_payload(frame_id="f-fed-2", sequence=2, media_url=remote),
        ),
    )
    assert await inbound._highlight_repo.list_frames("s-fed-1") == []


# ─── Round trip: local create → outbound → receiver ──────────────────────────


@pytest.fixture
async def author_db(tmp_dir):
    """The author's own household — a second, separate database."""
    database = AsyncDatabase(tmp_dir / "author.db", batch_timeout_ms=10)
    await database.startup()
    await database.enqueue(
        "INSERT INTO users(user_id, username, display_name) VALUES(?,?,?)",
        ("uid-remote", "alice", "Alice"),
    )
    yield database
    await database.shutdown()


@pytest.mark.parametrize(
    "media_url",
    [
        "api/media/x.webp",
        "/api/media/x.webp",
        "https://example.invalid/img.jpg",
        "//evil.example/x.webp",
        "javascript:alert(1)",
        "api/media/../secret",
    ],
)
async def test_every_locally_accepted_frame_lands_on_the_receiver(
    author_db, inbound, media_url
):
    """The author's household and the receiver apply one rule to
    ``media_url``: a frame the author's household accepts is landed by
    every receiver, and one the receiver would drop is refused at create
    time — so a highlight can never be visible at home and invisible on
    the other households (the federation-demo ``verify`` regression)."""
    author_bus = EventBus()
    frames: list[HighlightFrameAdded] = []
    author_bus.subscribe(HighlightFrameAdded, frames.append)
    service = HighlightService(
        SqliteHighlightRepo(author_db), SqliteUserRepo(author_db), author_bus
    )
    try:
        await service.create_or_append_frame(
            author_user_id="uid-remote",
            frame_type=HighlightFrameType.IMAGE,
            media_url=media_url,
        )
    except InvalidMediaRefError:
        accepted = False
    else:
        accepted = True

    # The wire payload exactly as the outbound builds it.
    federation = MagicMock()
    federation.own_instance_id = "peer-a"
    federation.send_event = AsyncMock()
    user_repo = MagicMock()
    user_repo.get_instance_for_user = AsyncMock(return_value="peer-a")
    fed_repo = MagicMock()
    peer = MagicMock()
    peer.id = "self"
    fed_repo.list_social_instances = AsyncMock(return_value=[peer])
    outbound = HighlightFederationOutbound(
        bus=author_bus,
        federation_service=federation,
        federation_repo=fed_repo,
        user_repo=user_repo,
    )
    for ev in frames:
        await outbound._on_frame_added(ev)
    sent = [c.kwargs for c in federation.send_event.call_args_list]
    assert len(sent) == (1 if accepted else 0)
    if not accepted:
        return

    await inbound._on_highlight_created(
        _event(FederationEventType.HIGHLIGHT_CREATED, sent[0]["payload"])
    )
    landed = await inbound._highlight_repo.list_frames(frames[0].highlight_id)
    assert [f.media_url for f in landed] == [media_url]
