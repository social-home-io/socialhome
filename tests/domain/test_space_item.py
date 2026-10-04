"""Tests for the member-publish item types and their required scopes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.space_item import (
    GENERIC_ITEM_TYPES,
    ITEM_TYPE_COMMENT,
    ITEM_TYPE_COMMENT_DELETE,
    ITEM_TYPE_COMMENT_EDIT,
    ITEM_TYPE_POST,
    ITEM_TYPE_POST_DELETE,
    ITEM_TYPE_POST_EDIT,
    ITEM_TYPE_REACTION_ADD,
    ITEM_TYPE_REACTION_REMOVE,
    MAX_ITEM_CLOCK_SKEW_S,
    POST_SHAPED_ITEM_TYPES,
    SUPPORTED_ITEM_TYPES,
    item_stamp,
    required_scope,
    stamp_to_db,
)
from socialhome.domain.writer_cert import WRITER_SCOPE_COMMENT, WRITER_SCOPE_WRITE


def test_the_two_inner_shapes_partition_the_supported_types():
    assert POST_SHAPED_ITEM_TYPES == {ITEM_TYPE_POST, ITEM_TYPE_POST_EDIT}
    assert not POST_SHAPED_ITEM_TYPES & GENERIC_ITEM_TYPES
    assert SUPPORTED_ITEM_TYPES == POST_SHAPED_ITEM_TYPES | GENERIC_ITEM_TYPES


@pytest.mark.parametrize(
    ("item_type", "scope"),
    [
        (ITEM_TYPE_POST, WRITER_SCOPE_WRITE),
        (ITEM_TYPE_POST_EDIT, WRITER_SCOPE_WRITE),
        (ITEM_TYPE_POST_DELETE, WRITER_SCOPE_WRITE),
        (ITEM_TYPE_COMMENT, WRITER_SCOPE_COMMENT),
        (ITEM_TYPE_COMMENT_EDIT, WRITER_SCOPE_COMMENT),
        (ITEM_TYPE_COMMENT_DELETE, WRITER_SCOPE_COMMENT),
        (ITEM_TYPE_REACTION_ADD, WRITER_SCOPE_COMMENT),
        (ITEM_TYPE_REACTION_REMOVE, WRITER_SCOPE_COMMENT),
    ],
)
def test_each_type_names_the_scope_it_needs(item_type, scope):
    assert required_scope(item_type) == scope


def test_an_unknown_type_has_no_scope():
    with pytest.raises(ValueError):
        required_scope("poll")


def test_item_stamp_parses_a_tz_aware_stamp_within_the_skew():
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    stamp = (now - timedelta(seconds=5)).isoformat()
    assert item_stamp(stamp, now=now) == now - timedelta(seconds=5)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        3,
        "",
        "not a date",
        "2026-10-03T12:00:00",  # naive — the wire field is tz-aware
        (
            datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
            + timedelta(seconds=MAX_ITEM_CLOCK_SKEW_S + 1)
        ).isoformat(),
    ],
)
def test_item_stamp_refuses_malformed_naive_and_future_stamps(raw):
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    assert item_stamp(raw, now=now) is None


def test_stamp_to_db_is_naive_utc_and_orders_like_sqlite_now():
    stamp = datetime(2026, 10, 3, 14, 0, 0, 250000, tzinfo=timezone(timedelta(hours=2)))
    assert stamp_to_db(stamp) == "2026-10-03 12:00:00.250000"
    # Compares as a string against SQLite's datetime('now') shape.
    assert "2026-10-03 12:00:00" < stamp_to_db(stamp)
