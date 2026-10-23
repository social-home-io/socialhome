"""Tests for the member-publish item types and their required scopes."""

from __future__ import annotations

import json
import os
import zlib
from datetime import UTC, datetime, timedelta, timezone

import pytest
from orjson import dumps as orjson_dumps

from socialhome.domain.item_zdict import ZDICT_V1 as ZDICT
from socialhome.domain.space_item import (
    AUTHORITY_KIND_APPROVED_POST,
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_REMOVAL,
    COMPRESSED_PAYLOAD_SUITES,
    GENERIC_ITEM_TYPES,
    ITEM_SIZE_BUCKETS,
    ITEM_TYPE_COMMENT,
    ITEM_TYPE_COMMENT_DELETE,
    ITEM_TYPE_COMMENT_EDIT,
    ITEM_TYPE_POST,
    ITEM_TYPE_POST_DELETE,
    ITEM_TYPE_POST_EDIT,
    ITEM_TYPE_REACTION_ADD,
    ITEM_TYPE_REACTION_REMOVE,
    MAX_INFLATED_ITEM_BYTES,
    MAX_ITEM_CLOCK_SKEW_S,
    PAD_FIELD,
    PAYLOAD_SUITE_JSON,
    PAYLOAD_SUITE_ZLIB,
    PAYLOAD_SUITE_ZLIB_DICT1,
    POST_SHAPED_ITEM_TYPES,
    REMOVAL_TARGET_COMMENT,
    REMOVAL_TARGET_POST,
    REMOVAL_TARGETS,
    SUPPORTED_AUTHORITY_KINDS,
    SUPPORTED_ITEM_TYPES,
    SUPPORTED_PAYLOAD_SUITES,
    AuthorityRemoval,
    UnsupportedPayloadSuite,
    item_stamp,
    open_item_plaintext,
    pad_json_object,
    payload_suite_of,
    required_scope,
    seal_item_plaintext,
    stamp_to_db,
    zlib_item_plaintext,
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
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
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
            datetime(2026, 10, 3, 12, tzinfo=UTC)
            + timedelta(seconds=MAX_ITEM_CLOCK_SKEW_S + 1)
        ).isoformat(),
    ],
)
def test_item_stamp_refuses_malformed_naive_and_future_stamps(raw):
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
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


def test_padding_takes_a_caller_bucket_ladder():
    """The GFS envelope relay pads to its own ladder (one rung above the
    item ladder) with the same helper."""
    ladder = (100, 300)
    assert len(pad_json_object({"a": 1}, buckets=ladder)) == 100
    assert len(pad_json_object({"a": "x" * 150}, buckets=ladder)) == 300
    over = pad_json_object({"a": "x" * 400}, buckets=ladder)
    assert len(over) > 300
    assert json.loads(over) == {"a": "x" * 400, PAD_FIELD: ""}


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


# ── Payload suites ───────────────────────────────────────────────────────

_POST_BODY = {
    "post_id": "p" * 32,
    "content": "Lovely morning at the allotment — Grüße 漢字 😀 " * 20,
    "author_pk": "ab" * 32,
    "author_sig": "s" * 86,
}


def test_payload_suite_of_defaults_and_rejects_unknown():
    assert payload_suite_of(None) == PAYLOAD_SUITE_JSON
    assert payload_suite_of("zlib1") == PAYLOAD_SUITE_ZLIB
    for bad in ("zlib2", "", 3, "JSON"):
        with pytest.raises(UnsupportedPayloadSuite):
            payload_suite_of(bad)
    assert payload_suite_of("zlib1+dict1") == PAYLOAD_SUITE_ZLIB_DICT1
    assert SUPPORTED_PAYLOAD_SUITES == {"json", "zlib1", "zlib1+dict1"}
    assert COMPRESSED_PAYLOAD_SUITES == ("zlib1+dict1", "zlib1")


def test_zlib_item_round_trips_and_pads_to_a_bucket():
    sealed = seal_item_plaintext(_POST_BODY, suite=PAYLOAD_SUITE_ZLIB)
    assert sealed is not None
    assert len(sealed) in ITEM_SIZE_BUCKETS
    assert len(sealed) < len(pad_json_object(_POST_BODY))
    assert json.loads(open_item_plaintext(sealed, PAYLOAD_SUITE_ZLIB)) == _POST_BODY


def test_zlib_item_drops_a_pad_the_body_carries():
    sealed = seal_item_plaintext({**_POST_BODY, PAD_FIELD: "000"}, suite="zlib1")
    assert sealed is not None
    assert PAD_FIELD not in json.loads(open_item_plaintext(sealed, "zlib1"))


def test_json_suite_is_pad_json_object_and_opens_as_is():
    sealed = seal_item_plaintext(_POST_BODY)
    assert sealed == pad_json_object(_POST_BODY)
    assert open_item_plaintext(sealed, PAYLOAD_SUITE_JSON) == sealed


def test_unknown_suite_is_refused_both_ways():
    with pytest.raises(UnsupportedPayloadSuite):
        seal_item_plaintext(_POST_BODY, suite="brotli")
    with pytest.raises(UnsupportedPayloadSuite):
        open_item_plaintext(b"{}", "brotli")


def test_an_item_too_large_to_inflate_safely_is_not_compressed():
    big = {"content": "x" * (MAX_INFLATED_ITEM_BYTES + 1)}
    assert zlib_item_plaintext(big) is None
    assert seal_item_plaintext(big, suite="zlib1") is None


def test_a_stream_over_the_top_bucket_is_not_compressed():
    noise = {"content": os.urandom(4096).hex()}
    assert zlib_item_plaintext(noise, buckets=(1024, 4096)) is None
    assert zlib_item_plaintext(noise) is not None


def test_a_decompression_bomb_is_refused_without_inflating_it():
    """A member holding the content key could seal a small stream that
    inflates to gigabytes. The receiver stops at the cap."""
    bomb = zlib.compress(b"0" * (64 * MAX_INFLATED_ITEM_BYTES), 9)
    assert len(bomb) < 16_384
    with pytest.raises(ValueError, match="cap"):
        open_item_plaintext(bomb, PAYLOAD_SUITE_ZLIB)


def test_exactly_the_cap_inflates():
    raw = b'"' + b"a" * (MAX_INFLATED_ITEM_BYTES - 2) + b'"'
    assert open_item_plaintext(zlib.compress(raw), "zlib1") == raw


def test_a_truncated_stream_is_refused():
    sealed = zlib.compress(orjson_dumps(_POST_BODY))
    with pytest.raises(ValueError, match="end"):
        open_item_plaintext(sealed[:-6], PAYLOAD_SUITE_ZLIB)


def test_data_after_the_stream_must_be_zero_padding():
    sealed = zlib.compress(orjson_dumps(_POST_BODY))
    assert open_item_plaintext(sealed + b"\0" * 50, "zlib1") == orjson_dumps(_POST_BODY)
    with pytest.raises(ValueError, match="after"):
        open_item_plaintext(sealed + b"\0\x01", PAYLOAD_SUITE_ZLIB)


def test_garbage_is_a_value_error():
    with pytest.raises(ValueError, match="corrupt"):
        open_item_plaintext(b"{not zlib}", PAYLOAD_SUITE_ZLIB)


# ── zlib1+dict1 (preset dictionary) ──────────────────────────────────────

_SHAPED_BODY = {
    "item_type": "post",
    "inner": {
        "post_id": "p" * 32,
        "space_id": "s" * 32,
        "author_user_id": "u" * 32,
        "author_pk": "ab" * 32,
        "author_username": "erin",
        "type": "text",
        "content": "Plums by the gate, help yourself.",
        "media_url": None,
        "image_urls": [],
        "created_at": "2026-10-23T09:30:00+00:00",
        "hidden_from_feed": False,
        "origin_instance_id": "o" * 32,
        "item_type": "post",
        "item_target": "p" * 32,
        "author_sig": "x" * 86,
    },
}


def test_a_dictionary_item_round_trips_and_is_smaller():
    sealed = seal_item_plaintext(_SHAPED_BODY, suite=PAYLOAD_SUITE_ZLIB_DICT1)
    assert sealed is not None and len(sealed) in ITEM_SIZE_BUCKETS
    assert json.loads(open_item_plaintext(sealed, "zlib1+dict1")) == _SHAPED_BODY
    raw = orjson_dumps(_SHAPED_BODY)
    assert len(zlib.compress(raw, 9)) - len(sealed.rstrip(b"\0")) > 100


@pytest.mark.security
def test_a_dictionary_stream_never_opens_as_plain_zlib_or_back():
    """The suite names the dictionary. A GFS flipping the routing-visible
    label gains nothing: a dictionary stream does not inflate as plain zlib
    (it names a dictionary the plain reader does not have), and a plain
    stream read under the dictionary suite is the same item (zlib uses the
    dictionary only when the stream asks for it). The author signature
    inside decides either way."""
    with_dict = seal_item_plaintext(_SHAPED_BODY, suite=PAYLOAD_SUITE_ZLIB_DICT1)
    plain = seal_item_plaintext(_SHAPED_BODY, suite=PAYLOAD_SUITE_ZLIB)
    assert with_dict is not None and plain is not None
    with pytest.raises(ValueError, match="corrupt"):
        open_item_plaintext(with_dict, PAYLOAD_SUITE_ZLIB)
    assert json.loads(open_item_plaintext(plain, PAYLOAD_SUITE_ZLIB_DICT1)) == (
        _SHAPED_BODY
    )


@pytest.mark.security
def test_a_dictionary_bomb_is_refused_at_the_same_cap():
    deflater = zlib.compressobj(9, zlib.DEFLATED, 15, 9, zlib.Z_DEFAULT_STRATEGY, ZDICT)
    bomb = deflater.compress(b"0" * (64 * MAX_INFLATED_ITEM_BYTES)) + deflater.flush()
    with pytest.raises(ValueError, match="cap"):
        open_item_plaintext(bomb, PAYLOAD_SUITE_ZLIB_DICT1)


def test_zlib_item_plaintext_refuses_an_unknown_suite():
    with pytest.raises(UnsupportedPayloadSuite):
        zlib_item_plaintext(_SHAPED_BODY, suite="json")
