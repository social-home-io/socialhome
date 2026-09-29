"""Tests for socialhome.domain.mention."""

from __future__ import annotations

from socialhome.domain.mention import (
    MentionCandidate,
    MentionParser,
    MentionType,
    candidate_lookup,
    mention_tokens,
)


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


# ─── Grammar (handles, emails, punctuation, @here) ─────────────────────────


def _parser(table: dict[str, str]) -> MentionParser:
    return MentionParser(lookup_member=lambda t, s: table.get(t.lower()))


def test_handle_with_dot_and_hyphen_is_one_token():
    """``@anna.b`` / ``@jo-ann`` resolve as whole handles, not a prefix."""
    out = _parser({"anna.b": "u1", "jo-ann": "u2"}).parse(
        "hi @anna.b and @jo-ann", "s1"
    )
    assert [m.user_id for m in out] == ["u1", "u2"]


def test_trailing_sentence_punctuation_is_not_part_of_token():
    out = _parser({"anna": "u1"}).parse("thanks @anna. and @anna!", "s1")
    assert [(m.raw, m.user_id) for m in out] == [("@anna", "u1")]


def test_email_address_is_not_a_mention():
    """``bob@example.com`` must never be read as a mention of ``example``."""
    out = _parser({"example": "u1"}).parse("mail bob@example.com", "s1")
    assert out == ()


def test_qualified_token_is_passed_whole_to_lookup():
    seen: list[str] = []

    def lookup(t, s):
        seen.append(t)
        return "u9" if t == "anna@k3f9x2" else None

    out = MentionParser(lookup_member=lookup).parse("hey @anna@k3f9x2!", "s1")
    assert seen == ["anna@k3f9x2"]
    assert out[0].raw == "@anna@k3f9x2" and out[0].user_id == "u9"


def test_here_does_not_swallow_user_mentions():
    """``@here @anna`` keeps the user mention — @here is not notified yet,
    so dropping ``@anna`` would silently lose her notification."""
    out = _parser({"anna": "u1"}).parse("@here and @anna @here", "s1")
    assert [m.type for m in out] == [MentionType.HERE, MentionType.USER]
    assert out[1].user_id == "u1"


def test_here_prefix_word_is_not_here():
    """``@hereford`` is a user token, not ``@here``."""
    out = _parser({"hereford": "u1"}).parse("@hereford", "s1")
    assert [m.type for m in out] == [MentionType.USER]


def test_unresolved_and_duplicate_tokens_dropped():
    out = _parser({"anna": "u1"}).parse("@anna @Anna @nobody @-x", "s1")
    assert [m.user_id for m in out] == ["u1"]


# ─── Candidate tokens + lookup ─────────────────────────────────────────────


def test_unique_handle_gets_bare_token_and_resolves():
    cands = [
        MentionCandidate(user_id="uaaaa1", handles=("anna",)),
        MentionCandidate(user_id="ubbbb2", handles=("bob", "robert")),
    ]
    tokens = mention_tokens(cands)
    assert tokens == {"uaaaa1": "anna", "ubbbb2": "bob"}
    lookup = candidate_lookup(cands)
    assert lookup("ANNA", "s") == "uaaaa1"
    # A secondary name (login username) also resolves when unique.
    assert lookup("robert", "s") == "ubbbb2"
    assert lookup("carol", "s") is None


def test_colliding_handle_is_qualified_and_bare_token_is_ambiguous():
    """Two households each have an ``anna``: the bare token resolves to
    nobody (silence over the wrong person), the qualified tokens resolve
    uniquely to each."""
    cands = [
        MentionCandidate(user_id="k3f9x2aaaa", handles=("anna",)),
        MentionCandidate(user_id="p7q2m1bbbb", handles=("anna",)),
    ]
    tokens = mention_tokens(cands)
    assert tokens["k3f9x2aaaa"] != tokens["p7q2m1bbbb"]
    lookup = candidate_lookup(cands)
    assert lookup("anna", "s") is None
    for uid, tok in tokens.items():
        assert tok is not None and tok.startswith("anna@")
        assert lookup(tok, "s") == uid
        parsed = MentionParser(lookup_member=lookup).parse(f"hi @{tok}.", "s")
        assert [m.user_id for m in parsed] == [uid]


def test_qualifier_grows_until_unique():
    cands = [
        MentionCandidate(user_id="abcdef111", handles=("anna",)),
        MentionCandidate(user_id="abcdef222", handles=("anna",)),
    ]
    tokens = mention_tokens(cands)
    assert tokens == {"abcdef111": "anna@abcdef1", "abcdef222": "anna@abcdef2"}


def test_handle_colliding_with_other_members_username_is_qualified():
    cands = [
        MentionCandidate(user_id="u1xxxxxx", handles=("anna",)),
        MentionCandidate(user_id="u2yyyyyy", handles=("annie", "anna")),
    ]
    tokens = mention_tokens(cands)
    assert tokens["u2yyyyyy"] == "annie"
    assert tokens["u1xxxxxx"] is not None
    assert tokens["u1xxxxxx"].startswith("anna@")
    assert candidate_lookup(cands)(tokens["u1xxxxxx"], "s") == "u1xxxxxx"


def test_untokenisable_handle_falls_back_then_none():
    cands = [
        MentionCandidate(user_id="u1", handles=("Anna Maria", "anna_m")),
        MentionCandidate(user_id="u2", handles=("<b>x</b>",)),
        MentionCandidate(user_id="u3", handles=("here",)),
    ]
    tokens = mention_tokens(cands)
    assert tokens == {"u1": "anna_m", "u2": None, "u3": None}


def test_qualifier_needs_word_chars_in_user_id():
    """A user_id whose prefix isn't token-safe can't be qualified → None."""
    cands = [
        MentionCandidate(user_id="a-b-c-1", handles=("anna",)),
        MentionCandidate(user_id="a-b-c-2", handles=("anna",)),
    ]
    assert mention_tokens(cands) == {"a-b-c-1": None, "a-b-c-2": None}


def test_qualified_token_for_unknown_prefix_is_none():
    cands = [MentionCandidate(user_id="k3f9x2", handles=("anna",))]
    lookup = candidate_lookup(cands)
    assert lookup("anna@zzzz", "s") is None
    assert lookup("anna@k3f9", "s") == "k3f9x2"
    assert lookup("", "s") is None
