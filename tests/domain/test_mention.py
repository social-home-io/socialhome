"""Tests for social_home.domain.mention."""

from __future__ import annotations

from social_home.domain.mention import MentionParser, MentionType


def test_parse_here():
    """@here mention is parsed as MentionType.HERE."""
    parser = MentionParser(lookup_member=lambda t, s: None)
    out = parser.parse("@here meeting now", "s1")
    assert len(out) == 1 and out[0].type is MentionType.HERE


def test_parse_users():
    """Named @mentions resolve to user IDs via lookup_member."""

    def lookup(t, s):
        return {"anna": "u1", "bob": "u2"}.get(t.lower())

    parser = MentionParser(lookup_member=lookup)
    out = parser.parse("hey @anna and @bob", "s1")
    assert len(out) == 2
    assert {m.user_id for m in out} == {"u1", "u2"}


def test_empty_content():
    """Parsing an empty string returns an empty tuple."""
    parser = MentionParser(lookup_member=lambda t, s: None)
    assert parser.parse("", "s1") == ()
