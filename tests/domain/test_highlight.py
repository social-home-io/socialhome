"""Smoke tests for the Highlight domain dataclasses."""

from __future__ import annotations

import dataclasses

from socialhome.domain.highlight import (
    Highlight,
    HighlightAudience,
    HighlightFrame,
    HighlightFrameReaction,
    HighlightFrameReplySnapshot,
    HighlightFrameType,
    HighlightFrameView,
    same_audience,
)


def test_highlight_defaults_audience_to_all_paired():
    s = Highlight(id="s1", author_user_id="u1", highlight_date="2026-05-03")
    assert s.audience_kind is HighlightAudience.ALL_PAIRED
    assert s.audience == ()


def test_highlight_frame_type_enum_round_trip():
    f = HighlightFrame(
        id="f1",
        highlight_id="s1",
        sequence=1,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    assert f.frame_type.value == "image"
    assert HighlightFrameType("video") is HighlightFrameType.VIDEO


def test_highlight_view_and_reaction_carry_only_keys():
    v = HighlightFrameView(frame_id="f1", viewer_user_id="u2")
    r = HighlightFrameReaction(frame_id="f1", reactor_user_id="u2", emoji="🔥")
    assert v.frame_id == "f1"
    assert r.emoji == "🔥"


def test_reply_snapshot_is_frozen():
    snap = HighlightFrameReplySnapshot(
        thumb_url="/api/media/x.webp",
        author_user_id="u1",
        highlight_date="2026-05-03",
    )
    assert dataclasses.is_dataclass(snap)
    try:
        snap.thumb_url = "/other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("HighlightFrameReplySnapshot must be frozen")


def test_same_audience():
    a, u, h = (
        HighlightAudience.ALL_PAIRED,
        HighlightAudience.USERS,
        HighlightAudience.HOUSEHOLDS,
    )
    assert same_audience(a, (), a, ("ignored",))
    assert same_audience(u, ("x", "y"), u, ("y", "x"))
    assert not same_audience(u, ("x",), u, ("x", "y"))
    assert not same_audience(u, ("x",), h, ("x",))
    assert not same_audience(a, (), u, ())
