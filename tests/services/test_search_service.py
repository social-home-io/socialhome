"""Tests for SearchService — domain-event-driven indexing."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    PostCreated,
    PostDeleted,
    PostEdited,
    SpacePostCreated,
)
from datetime import datetime, timezone

from socialhome.domain.post import Post, PostType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.search_repo import (
    SCOPE_POST,
    SCOPE_SPACE_POST,
    SqliteSearchRepo,
)
from socialhome.services.search_service import SearchService


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    bus = EventBus()
    repo = SqliteSearchRepo(db)
    svc = SearchService(bus, repo)
    svc.wire()
    yield svc, bus, repo
    await db.shutdown()


def _post(post_id: str, content: str) -> Post:
    return Post(
        id=post_id,
        author="u1",
        type=PostType.TEXT,
        content=content,
        created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )


async def test_post_created_event_indexes_post(env):
    svc, bus, _ = env
    await bus.publish(PostCreated(post=_post("p1", "search me")))
    hits = await svc.search("search")
    assert len(hits) == 1
    assert hits[0].ref_id == "p1"


async def test_post_edited_event_replaces_index_entry(env):
    svc, bus, _ = env
    await bus.publish(PostCreated(post=_post("p1", "old text")))
    await bus.publish(PostEdited(post=_post("p1", "new text")))
    assert await svc.search("old") == []
    new_hits = await svc.search("new")
    assert len(new_hits) == 1


async def test_post_deleted_event_removes_index_entry(env):
    svc, bus, _ = env
    await bus.publish(PostCreated(post=_post("p1", "to be removed")))
    await bus.publish(PostDeleted(post_id="p1"))
    assert await svc.search("removed") == []


async def test_space_post_created_indexes_with_space_scope(env):
    svc, bus, _ = env
    await bus.publish(
        SpacePostCreated(post=_post("sp1", "secret recipe"), space_id="sp-A")
    )
    hits = await svc.search("recipe")
    assert len(hits) == 1
    assert hits[0].scope == SCOPE_SPACE_POST
    assert hits[0].space_id == "sp-A"


async def test_search_filters_by_scope(env):
    svc, bus, _ = env
    await bus.publish(PostCreated(post=_post("p1", "needle in feed")))
    await bus.publish(
        SpacePostCreated(post=_post("sp1", "needle in space"), space_id="sp-A")
    )
    only_feed = await svc.search("needle", scope=SCOPE_POST)
    assert {h.ref_id for h in only_feed} == {"p1"}


async def test_post_with_empty_content_is_not_indexed(env):
    """File posts with no caption should be skipped — they have no text."""
    svc, bus, _ = env
    await bus.publish(PostCreated(post=_post("p1", "")))
    assert await svc.search("anything") == []


# ─── Page indexing (§18) ──────────────────────────────────────────────────


async def test_page_created_event_indexes_page(env):
    from socialhome.domain.events import PageCreated
    from socialhome.repositories.search_repo import SCOPE_PAGE

    svc, bus, _ = env
    await bus.publish(
        PageCreated(
            page_id="page-1",
            space_id=None,
            title="Grocery notes",
            content="flour sugar butter",
        )
    )
    hits = await svc.search("butter")
    assert len(hits) == 1
    assert hits[0].scope == SCOPE_PAGE
    assert hits[0].ref_id == "page-1"


async def test_page_updated_event_replaces_index_entry(env):
    from socialhome.domain.events import PageCreated, PageUpdated

    svc, bus, _ = env
    await bus.publish(
        PageCreated(
            page_id="page-2",
            space_id=None,
            title="t",
            content="original text",
        )
    )
    await bus.publish(
        PageUpdated(
            page_id="page-2",
            space_id=None,
            title="t",
            content="refreshed body",
        )
    )
    assert (await svc.search("original")) == []
    assert [h.ref_id for h in await svc.search("refreshed")] == ["page-2"]


async def test_page_deleted_event_removes_index_entry(env):
    from socialhome.domain.events import PageCreated, PageDeleted

    svc, bus, _ = env
    await bus.publish(
        PageCreated(
            page_id="page-3",
            space_id=None,
            title="t",
            content="doomed page",
        )
    )
    await bus.publish(PageDeleted(page_id="page-3"))
    assert (await svc.search("doomed")) == []


async def test_empty_page_not_indexed(env):
    from socialhome.domain.events import PageCreated

    svc, bus, _ = env
    await bus.publish(
        PageCreated(
            page_id="page-4",
            space_id=None,
            title="",
            content="",
        )
    )
    assert await svc.search("anything") == []


# ─── DM messages ──────────────────────────────────────────────────────────


async def test_dm_message_is_indexed_with_scope_message(env):
    from socialhome.domain.events import DmMessageCreated
    from socialhome.repositories.search_repo import SCOPE_MESSAGE

    svc, bus, _ = env
    await bus.publish(
        DmMessageCreated(
            conversation_id="c-1",
            message_id="m-1",
            sender_user_id="u1",
            sender_display_name="Anna",
            recipient_user_ids=("u2",),
            content="meet at the library tomorrow",
        )
    )
    hits = await svc.search("library")
    assert len(hits) == 1
    assert hits[0].scope == SCOPE_MESSAGE
    assert hits[0].ref_id == "m-1"


async def test_empty_dm_message_not_indexed(env):
    from socialhome.domain.events import DmMessageCreated

    svc, bus, _ = env
    await bus.publish(
        DmMessageCreated(
            conversation_id="c-1",
            message_id="m-2",
            sender_user_id="u1",
            sender_display_name="Anna",
            recipient_user_ids=("u2",),
            content="",
        )
    )
    assert await svc.search("x") == []
