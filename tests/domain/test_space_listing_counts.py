"""Tests for :mod:`socialhome.domain.space_listing_counts`."""

from __future__ import annotations

import pytest

from socialhome.domain.space_listing_counts import (
    MAX_LISTING_COUNT,
    SpaceListingCounts,
    parse_listing_count,
)


@pytest.mark.parametrize("value", [0, 1, 42, MAX_LISTING_COUNT])
def test_accepts_sane_ints(value: int) -> None:
    assert parse_listing_count(value) == value


@pytest.mark.parametrize(
    "value",
    [None, -1, MAX_LISTING_COUNT + 1, True, False, 3.0, "3", [], {}],
)
def test_rejects_everything_else(value: object) -> None:
    assert parse_listing_count(value) is None


def test_counts_are_frozen() -> None:
    counts = SpaceListingCounts(member_count=3, follower_count=7)
    with pytest.raises(AttributeError):
        counts.member_count = 4  # type: ignore[misc]
