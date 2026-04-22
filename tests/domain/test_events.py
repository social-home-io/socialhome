"""Tests for socialhome.domain.events."""

from __future__ import annotations

from datetime import datetime, timezone

from socialhome.domain.events import DomainEvent, PostCreated
from socialhome.domain.post import Post, PostType


def test_post_created_is_domain_event():
    """PostCreated is a DomainEvent with a valid occurred_at timestamp."""
    now = datetime.now(timezone.utc)
    p = Post(id="p1", author="u1", type=PostType.TEXT, created_at=now)
    e = PostCreated(post=p)
    assert isinstance(e, DomainEvent)
    assert e.occurred_at is not None
