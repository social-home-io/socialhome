"""Tests for social_home.infrastructure.event_bus."""

from __future__ import annotations

from datetime import datetime, timezone


from social_home.domain.events import PostCreated
from social_home.domain.post import Post, PostType
from social_home.infrastructure.event_bus import EventBus


async def test_publish_delivers():
    """A published event is received by all subscribed handlers."""
    bus = EventBus()
    seen = []

    async def handler(e):
        seen.append(e.post.id)

    bus.subscribe(PostCreated, handler)
    now = datetime.now(timezone.utc)
    await bus.publish(
        PostCreated(post=Post(id="p1", author="u", type=PostType.TEXT, created_at=now))
    )
    assert seen == ["p1"]


async def test_handler_error_isolation():
    """A failing handler does not prevent other handlers from running."""
    bus = EventBus()
    seen = []

    async def good(e):
        seen.append("ok")

    async def bad(e):
        raise RuntimeError("boom")

    bus.subscribe(PostCreated, bad)
    bus.subscribe(PostCreated, good)
    now = datetime.now(timezone.utc)
    await bus.publish(
        PostCreated(post=Post(id="p", author="u", type=PostType.TEXT, created_at=now))
    )
    assert seen == ["ok"]


async def test_unsubscribe():
    """Unsubscribing a handler removes it so handler_count drops to zero."""
    bus = EventBus()

    async def handler(e):
        pass

    bus.subscribe(PostCreated, handler)
    assert bus.handler_count(PostCreated) == 1
    bus.unsubscribe(PostCreated, handler)
    assert bus.handler_count(PostCreated) == 0


async def test_unsubscribe_nonexistent_is_noop():
    """Unsubscribing a handler that was never subscribed does not raise."""
    bus = EventBus()

    async def handler(e):
        pass

    bus.unsubscribe(PostCreated, handler)  # no error


async def test_handler_count_zero_for_unknown_type():
    """handler_count returns 0 for an event type with no subscribers."""
    bus = EventBus()
    assert bus.handler_count(PostCreated) == 0
