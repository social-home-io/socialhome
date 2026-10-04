"""Tests for the member-publish item types and their required scopes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.space_item import (
    AUTHORITY_KIND_APPROVED_POST,
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_REMOVAL,
    GENERIC_ITEM_TYPES,
    ITEM_SIZE_BUCKETS,
    PAD_FIELD,
    REMOVAL_TARGET_COMMENT,
    REMOVAL_TARGET_POST,
    REMOVAL_TARGETS,
    SUPPORTED_AUTHORITY_KINDS,
    AuthorityRemoval,
    pad_json_object,
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


# ─── Size padding + the authority-only removal notice ────────────────────


def test_padding_lands_on_a_bucket_and_keeps_the_body():
    body = {"a": "x" * 10}
    pt = pad_json_object(body)
    assert len(pt) == ITEM_SIZE_BUCKETS[0]
    decoded = json.loads(pt)
    assert decoded["a"] == "x" * 10
    assert set(decoded) == {"a", PAD_FIELD}
    # The caller's dict is never mutated.
    assert body == {"a": "x" * 10}


def test_padding_replaces_a_pad_the_body_already_carries():
    pt = pad_json_object({"a": 1, PAD_FIELD: "0" * 5000})
    assert len(pt) == ITEM_SIZE_BUCKETS[0]


def test_padding_moves_up_a_bucket_and_leaves_huge_bodies_alone():
    assert len(pad_json_object({"a": "x" * 2000})) == ITEM_SIZE_BUCKETS[1]
    huge = pad_json_object({"a": "x" * (ITEM_SIZE_BUCKETS[-1] + 10)})
    assert len(huge) > ITEM_SIZE_BUCKETS[-1]


def test_removal_round_trips_through_its_inner():
    removal = AuthorityRemoval(
        space_id="sp", target=REMOVAL_TARGET_COMMENT, item_id="c1", post_id="p1"
    )
    inner = removal.to_inner()
    assert inner == {
        AUTHORITY_KIND_FIELD: AUTHORITY_KIND_REMOVAL,
        "space_id": "sp",
        "target": "comment",
        "item_id": "c1",
        "post_id": "p1",
    }
    assert AuthorityRemoval.from_inner(inner) == removal


def test_a_removal_may_name_the_author():
    removal = AuthorityRemoval(
        space_id="sp",
        target=REMOVAL_TARGET_POST,
        item_id="p1",
        post_id="p1",
        author_user_id="u1",
    )
    inner = removal.to_inner()
    assert inner["author_user_id"] == "u1"
    assert AuthorityRemoval.from_inner(inner) == removal
    with pytest.raises(ValueError):
        AuthorityRemoval.from_inner({**inner, "author_user_id": 5})
    with pytest.raises(ValueError):
        AuthorityRemoval.from_inner({**inner, "author_user_id": "x" * 200})


def test_a_post_removal_names_its_own_post():
    with pytest.raises(ValueError):
        AuthorityRemoval(
            space_id="sp", target=REMOVAL_TARGET_POST, item_id="p1", post_id="p2"
        )


@pytest.mark.parametrize(
    "mutate",
    [
        {AUTHORITY_KIND_FIELD: AUTHORITY_KIND_APPROVED_POST},
        {AUTHORITY_KIND_FIELD: None},
        {"target": "reaction"},
        {"item_id": ""},
        {"item_id": "x" * 200},
        {"post_id": 5},
        {"space_id": ""},
    ],
)
def test_a_malformed_removal_is_refused(mutate):
    inner = AuthorityRemoval(
        space_id="sp", target=REMOVAL_TARGET_POST, item_id="p1", post_id="p1"
    ).to_inner()
    inner.update(mutate)
    with pytest.raises(ValueError):
        AuthorityRemoval.from_inner(inner)


def test_the_authority_kinds_are_disjoint_from_the_member_item_types():
    """A member's ``space_item`` can never be read as an authority notice."""
    assert SUPPORTED_AUTHORITY_KINDS == {
        AUTHORITY_KIND_REMOVAL,
        AUTHORITY_KIND_APPROVED_POST,
    }
    assert not SUPPORTED_AUTHORITY_KINDS & SUPPORTED_ITEM_TYPES
    assert REMOVAL_TARGETS == {REMOVAL_TARGET_POST, REMOVAL_TARGET_COMMENT}
