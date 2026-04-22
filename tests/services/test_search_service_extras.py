"""Extra coverage for SearchService — moderation + comment-noop branches."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    CommentAdded,
    SpacePostModerated,
)
from socialhome.domain.post import (
    Comment,
    CommentType,
    Post,
    PostType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.search_repo import SqliteSearchRepo
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


def _post(pid: str, content: str):
    return Post(
        id=pid,
        author="u1",
        type=PostType.TEXT,
        content=content,
        created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )


async def test_comment_added_does_not_index_separately(env):
    """Comments are NOT independently indexed — only the post body."""
    svc, bus, _ = env
    comment = Comment(
        id="c1",
        post_id="p1",
        author="u1",
        type=CommentType.TEXT,
        content="comment-only-text",
        created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )
    await bus.publish(CommentAdded(post_id="p1", comment=comment))
    hits = await svc.search("comment-only-text")
    # No standalone comment index — query returns nothing.
    assert hits == []


async def test_space_post_moderated_drops_index_entry(env):
    svc, bus, _ = env
    from socialhome.domain.events import SpacePostCreated

    await bus.publish(
        SpacePostCreated(post=_post("sp1", "moderated text"), space_id="sp-A")
    )
    assert (await svc.search("moderated"))[0].ref_id == "sp1"
    await bus.publish(
        SpacePostModerated(
            space_id="sp-A",
            post=_post("sp1", "moderated text"),
            moderated_by="admin",
        )
    )
    # Index entry removed.
    assert await svc.search("moderated") == []
