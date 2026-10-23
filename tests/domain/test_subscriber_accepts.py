"""Tests for socialhome.domain.subscriber_accepts."""

from __future__ import annotations

import pytest

from socialhome.domain.subscriber_accepts import (
    ACCEPTS_AUTHOR_PROFILES_BUNDLE,
    ACCEPTS_PAYLOAD_ZLIB1,
    ACCEPTS_PAYLOAD_ZLIB1_DICT1,
    MAX_ACCEPTS_LABEL_CHARS,
    MAX_ACCEPTS_LABELS,
    OUR_ACCEPTS,
    SEAT_BIT_FOR_SUITE,
    parse_accepts,
    seat_bits,
)


def test_our_accepts_include_the_bundle():
    assert ACCEPTS_AUTHOR_PROFILES_BUNDLE in OUR_ACCEPTS
    # It reads zlib-compressed items (payload_suite "zlib1").
    assert ACCEPTS_PAYLOAD_ZLIB1 == "payload_zlib1"
    assert ACCEPTS_PAYLOAD_ZLIB1 in OUR_ACCEPTS
    assert parse_accepts(list(OUR_ACCEPTS)) == OUR_ACCEPTS


def test_the_dictionary_label_is_its_own_seat_bit():
    """``zlib1+dict1`` readers say so on every subscribe; the GFS keeps it
    in bit 4 and gates that suite on it."""
    assert ACCEPTS_PAYLOAD_ZLIB1_DICT1 == "payload_zlib1_dict1"
    assert ACCEPTS_PAYLOAD_ZLIB1_DICT1 in OUR_ACCEPTS
    assert seat_bits(OUR_ACCEPTS) == 7
    assert seat_bits(("payload_zlib1_dict1",)) == 4
    assert SEAT_BIT_FOR_SUITE == {"zlib1": 2, "zlib1+dict1": 4}


def test_missing_is_empty():
    assert parse_accepts(None) == ()


def test_unknown_labels_are_kept_and_deduped_in_order():
    assert parse_accepts(["b_2", "a", "b_2"]) == ("b_2", "a")


@pytest.mark.parametrize(
    "raw",
    ["author_profiles_bundle", {"author_profiles_bundle": True}, 7, True],
)
def test_a_non_list_is_empty_never_an_error(raw):
    """``accepts`` is an unsigned hint: a bad shape must never fail the
    subscribe it rides on."""
    assert parse_accepts(raw) == ()


def test_malformed_labels_are_dropped_and_the_rest_kept():
    raw = [1, "", "Has-Caps", "x" * (MAX_ACCEPTS_LABEL_CHARS + 1), "ok_1"]
    assert parse_accepts(raw) == ("ok_1",)


def test_at_most_the_max_labels_are_kept():
    raw = [f"l{i}" for i in range(MAX_ACCEPTS_LABELS + 5)]
    assert parse_accepts(raw) == tuple(raw[:MAX_ACCEPTS_LABELS])
