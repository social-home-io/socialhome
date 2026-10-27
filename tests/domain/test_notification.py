"""Tests for socialhome.domain.notification."""

from __future__ import annotations

from socialhome.domain.notification import ACTION_ITEM_TYPES, Notification


def test_is_read_follows_read_at():
    note = Notification(id="n", user_id="u", type="t", title="t", created_at="x")
    assert not note.is_read
    assert Notification(
        id="n", user_id="u", type="t", title="t", created_at="x", read_at="y"
    ).is_read


def test_action_items_are_the_review_queue_rows():
    """Join requests, posts waiting for review and reports — the rows the
    prune keeps while unread and a handling admin resolves for all."""
    assert ACTION_ITEM_TYPES == {
        "space_join_requested",
        "moderation_pending",
        "space_report",
    }
