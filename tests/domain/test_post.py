"""Tests for socialhome.domain.post."""

from __future__ import annotations

from datetime import date, datetime, timezone, time

import pytest

from socialhome.domain.post import (
    Availability,
    Comment,
    CommentType,
    Poll,
    PollData,
    PollOption,
    Post,
    PostType,
    SchedulePoll,
    ScheduleSlot,
)


def test_post_reaction_add_remove():
    """Adding and removing reactions updates the reactions mapping correctly."""
    now = datetime.now(timezone.utc)
    p = Post(id="p1", author="u1", type=PostType.TEXT, created_at=now)
    p2 = p.with_reaction("👍", "u1").with_reaction("👍", "u2")
    assert p2.reactions["👍"] == frozenset({"u1", "u2"})
    p3 = p2.without_reaction("👍", "u1")
    assert p3.reactions["👍"] == frozenset({"u2"})
    p4 = p3.without_reaction("👍", "u2")
    assert "👍" not in p4.reactions


def test_post_soft_delete():
    """soft_delete clears content but preserves comment_count and pinned flag."""
    now = datetime.now(timezone.utc)
    p = Post(
        id="p1",
        author="u1",
        type=PostType.TEXT,
        created_at=now,
        content="hi",
        comment_count=3,
        pinned=True,
    )
    d = p.soft_delete()
    assert d.deleted and d.content is None and d.comment_count == 3


def test_post_edit():
    """edit() replaces content and stamps edited_at."""
    now = datetime.now(timezone.utc)
    p = Post(id="p1", author="u1", type=PostType.TEXT, created_at=now, content="v1")
    p2 = p.edit("v2")
    assert p2.content == "v2" and p2.edited_at is not None


def test_post_increment_decrement_comments():
    """Comment count increments/decrements correctly and is clamped to zero."""
    now = datetime.now(timezone.utc)
    p = Post(id="p", author="u", type=PostType.TEXT, created_at=now)
    p2 = p.increment_comments().increment_comments()
    assert p2.comment_count == 2
    p3 = p2.decrement_comments().decrement_comments().decrement_comments()
    assert p3.comment_count == 0


def test_comment_soft_delete():
    """soft_delete clears content and media_url."""
    now = datetime.now(timezone.utc)
    c = Comment(
        id="c",
        post_id="p",
        author="u",
        type=CommentType.TEXT,
        created_at=now,
        content="hi",
        media_url="/img.jpg",
    )
    d = c.soft_delete()
    assert d.deleted and d.content is None and d.media_url is None


def test_poll_cast_and_retract():
    """Casting a vote updates counts; retracting removes it."""
    opts = (PollOption(id="a", text="Yes"), PollOption(id="b", text="No"))
    p = Poll(id="p", question="ok?", options=opts)
    p2 = p.cast_vote("u1", "a")
    assert p2.vote_count("a") == 1
    p3 = p2.cast_vote("u1", "b")
    assert p3.vote_count("a") == 0 and p3.vote_count("b") == 1
    p4 = p3.retract_vote("u1")
    assert p4.vote_count("b") == 0


def test_poll_closed_poll_rejects_vote():
    """Voting on a closed poll raises ValueError."""
    p = Poll(id="p", question="?", options=(PollOption(id="a", text="Y"),), closed=True)
    with pytest.raises(ValueError, match="closed"):
        p.cast_vote("u1", "a")


def test_poll_unknown_option():
    """Voting for an unknown option raises ValueError."""
    p = Poll(id="p", question="?", options=(PollOption(id="a", text="Y"),))
    with pytest.raises(ValueError, match="Unknown"):
        p.cast_vote("u1", "zzz")


def test_schedule_poll_with_response_and_finalize():
    """Responding to a slot populates the summary; finalizing closes the poll."""
    slots = (ScheduleSlot(id="s1", slot_date=date(2026, 5, 1), position=0),)
    sp = SchedulePoll(id="sp", title="Dinner?", slots=slots)
    sp2 = sp.with_response("u1", "s1", Availability.YES)
    assert sp2.response_summary()["s1"][Availability.YES] == 1
    sp3 = sp2.finalize("s1")
    assert sp3.closed and sp3.finalized_slot_id == "s1"
    with pytest.raises(ValueError, match="finalized"):
        sp3.with_response("u2", "s1", Availability.NO)


def test_schedule_poll_slot_label_with_time():
    """ScheduleSlot label includes start and end times when present."""
    s = ScheduleSlot(
        id="s",
        slot_date=date(2026, 5, 1),
        position=0,
        start_time=time(19, 0),
        end_time=time(21, 0),
    )
    label = s.label()
    assert "19:00" in label and "21:00" in label


def test_schedule_poll_slot_label_date_only():
    """ScheduleSlot label shows just the date when no time is provided."""
    s = ScheduleSlot(id="s", slot_date=date(2026, 5, 1), position=0)
    assert ":" not in s.label()


def test_schedule_poll_poll_data():
    """PollData is a storage/wire format with question and options."""
    pd = PollData(question="ok?", options=(PollOption(id="a", text="Y"),))
    assert pd.question == "ok?"
