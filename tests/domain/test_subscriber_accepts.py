"""Tests for socialhome.domain.subscriber_accepts."""

from __future__ import annotations

import pytest

from socialhome.domain.subscriber_accepts import (
    ACCEPTS_AUTHOR_PROFILES_BUNDLE,
    MAX_ACCEPTS_LABEL_CHARS,
    MAX_ACCEPTS_LABELS,
    OUR_ACCEPTS,
    parse_accepts,
)


def test_our_accepts_include_the_bundle():
    assert ACCEPTS_AUTHOR_PROFILES_BUNDLE in OUR_ACCEPTS
    assert parse_accepts(list(OUR_ACCEPTS)) == OUR_ACCEPTS


def test_missing_is_empty():
    assert parse_accepts(None) == ()


def test_unknown_labels_are_kept_and_deduped_in_order():
    assert parse_accepts(["b_2", "a", "b_2"]) == ("b_2", "a")


@pytest.mark.parametrize(
    "raw",
    [
        "author_profiles_bundle",  # not a list
        {"author_profiles_bundle": True},
        [1],
        [""],
        ["Has-Caps"],
        ["x" * (MAX_ACCEPTS_LABEL_CHARS + 1)],
        [f"l{i}" for i in range(MAX_ACCEPTS_LABELS + 1)],
    ],
)
def test_malformed_raises(raw):
    with pytest.raises(ValueError):
        parse_accepts(raw)
