"""Tests for socialhome.domain.gfs_hello_accepts."""

from __future__ import annotations

import pytest

from socialhome.domain.gfs_hello_accepts import (
    ACCEPTS_ENVELOPE_MEDIA,
    ACCEPTS_SPACE_MEDIA_SERVE,
    ACCEPTS_WS_BINARY1,
    MAX_ACCEPTS_LABEL_CHARS,
    MAX_ACCEPTS_LABELS,
    OUR_HELLO_ACCEPTS,
    parse_accepts,
)


def test_the_hello_lists_only_the_per_connection_labels():
    """v_96 — no space-keyed seat, so none of its labels: the hello asks
    for binary frames, answers media requests and opens relay media."""
    assert OUR_HELLO_ACCEPTS == (
        ACCEPTS_WS_BINARY1,
        ACCEPTS_SPACE_MEDIA_SERVE,
        ACCEPTS_ENVELOPE_MEDIA,
    )
    for retired in (
        "space_key_pull",
        "space_item",
        "payload_zlib1",
        "payload_zlib1_dict1",
        "author_profiles_bundle",
    ):
        assert retired not in OUR_HELLO_ACCEPTS
    assert parse_accepts(list(OUR_HELLO_ACCEPTS)) == OUR_HELLO_ACCEPTS


def test_missing_is_empty():
    assert parse_accepts(None) == ()


def test_unknown_labels_are_kept_and_deduped_in_order():
    assert parse_accepts(["b_2", "a", "b_2"]) == ("b_2", "a")


@pytest.mark.parametrize(
    "raw",
    ["ws_binary1", {"ws_binary1": True}, 7, True],
)
def test_a_non_list_is_empty_never_an_error(raw):
    """``accepts`` is an unsigned hint: a bad shape must never fail the
    hello it rides on."""
    assert parse_accepts(raw) == ()


def test_malformed_labels_are_dropped_and_the_rest_kept():
    raw = [1, "", "Has-Caps", "x" * (MAX_ACCEPTS_LABEL_CHARS + 1), "ok_1"]
    assert parse_accepts(raw) == ("ok_1",)


def test_at_most_the_max_labels_are_kept():
    raw = [f"l{i}" for i in range(MAX_ACCEPTS_LABELS + 5)]
    assert parse_accepts(raw) == tuple(raw[:MAX_ACCEPTS_LABELS])
