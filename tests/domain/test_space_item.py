"""Tests for the member-publish item types and their required scopes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from socialhome.domain import space_item
from socialhome.domain.space import SpaceFeatures
from socialhome.domain.space_item import _REQUIRED_SCOPE as REQUIRED_SCOPE_TABLE
from socialhome.domain.space_item import (
    AUTHORITY_KIND_APPROVED_POST,
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_REMOVAL,
    GENERIC_ITEM_TYPES,
    ITEM_SIZE_BUCKETS,
    ITEM_SPECS,
    ITEM_TYPE_COMMENT,
    ITEM_TYPE_COMMENT_DELETE,
    ITEM_TYPE_COMMENT_EDIT,
    ITEM_TYPE_POST,
    ITEM_TYPE_POST_DELETE,
    ITEM_TYPE_POST_EDIT,
    ITEM_TYPE_REACTION_ADD,
    ITEM_TYPE_REACTION_REMOVE,
    MAX_ITEM_CLOCK_SKEW_S,
    PAD_FIELD,
    POST_SHAPED_ITEM_TYPES,
    REMOVAL_TARGET_COMMENT,
    REMOVAL_TARGET_POST,
    REMOVAL_TARGETS,
    SUPPORTED_AUTHORITY_KINDS,
    SUPPORTED_ITEM_TYPES,
    AuthorityRemoval,
    item_stamp,
    pad_json_object,
    reaction_emoji_ok,
    reader_cert_permits,
    required_scope,
    stamp_to_db,
    stored_stamp,
)
from socialhome.domain.writer_cert import (
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_WRITE,
    WriterCert,
)


def _reader_cert(*, reader: bool) -> WriterCert:
    return WriterCert(
        cert_suite="ed25519",
        space_id="sp-1",
        epoch=1,
        instance_pk="A" * 43,
        scope=WRITER_SCOPE_COMMENT,
        issued_at=1,
        cert_sig="sig",
        reader=reader,
    )


#: What a reader cert may carry, per flag pair (comment, react).
_READER_ALLOWED: dict[str, tuple[bool, bool]] = {
    ITEM_TYPE_COMMENT: (True, False),
    ITEM_TYPE_COMMENT_EDIT: (True, False),
    ITEM_TYPE_REACTION_ADD: (False, True),
    ITEM_TYPE_REACTION_REMOVE: (False, True),
}


@pytest.mark.parametrize("item_type", sorted(REQUIRED_SCOPE_TABLE))
@pytest.mark.parametrize("comment", [False, True])
@pytest.mark.parametrize("react", [False, True])
def test_reader_cert_permits_every_item_type(item_type, comment, react):
    features = SpaceFeatures(
        allow_subscriber_comment=comment, allow_subscriber_react=react
    )
    # A non-reader cert is never narrowed by the follower switches.
    assert reader_cert_permits(_reader_cert(reader=False), item_type, features)
    if item_type == ITEM_TYPE_COMMENT_DELETE:
        expected = True  # the author's own delete: always (owner F6)
    elif item_type in _READER_ALLOWED:
        needs_comment, needs_react = _READER_ALLOWED[item_type]
        expected = (needs_comment and comment) or (needs_react and react)
    else:
        # Posts and their edits / deletes, and (v_96) the author profile —
        # a follower is named by the roster: never a reader.
        expected = False
    assert reader_cert_permits(_reader_cert(reader=True), item_type, features) is (
        expected
    )


def test_reader_cert_refuses_an_unknown_item_type():
    features = SpaceFeatures(allow_subscriber_comment=True, allow_subscriber_react=True)
    assert not reader_cert_permits(_reader_cert(reader=True), "bogus", features)


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


#: M5 — the post-shaped v_97 types, whose authors the ``posts`` access level
#: decides: the only ones a ``write`` cert must carry.
_POST_SHAPED_V97 = {
    "space_post_created",
    "space_post_updated",
    "space_post_deleted",
    "space_poll_created",
    "space_poll_closed",
    "space_schedule_created",
    "space_schedule_finalized",
    "bazaar_listing_created",
    "bazaar_listing_updated",
    "bazaar_listing_faq_updated",
    "bazaar_offer_accepted",
}


def test_only_post_shaped_items_need_a_write_cert():
    """M5 — a member under moderated posts holds a ``comment`` cert: it
    must still chat, vote, RSVP, share a pin and work on tasks in a GFS
    space (each feature's own access level is the receiver's handler's to
    judge, as for the legacy event)."""
    for item_type, spec in ITEM_SPECS.items():
        want = (
            WRITER_SCOPE_WRITE
            if item_type in _POST_SHAPED_V97
            else WRITER_SCOPE_COMMENT
        )
        assert spec.scope == want, item_type


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


def test_stamp_to_db_reads_a_naive_stamp_as_utc():
    """A stored stamp read back is naive UTC: written again it keeps its
    time, whatever this machine's time zone."""
    naive = datetime(2026, 10, 3, 12, 0, 0, 250000)
    assert stamp_to_db(naive) == "2026-10-03 12:00:00.250000"
    assert stamp_to_db(naive) == stamp_to_db(naive.replace(tzinfo=UTC))


def test_stored_stamp_reads_naive_as_utc_and_keeps_the_skew_rule():
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    assert stored_stamp("2026-10-03 11:00:00.500000", now=now) == datetime(
        2026, 10, 3, 11, 0, 0, 500000, tzinfo=UTC
    )
    assert stored_stamp("2026-10-03T13:00:00+02:00", now=now) == now - timedelta(
        hours=1
    )
    # Years ahead would win every later write (last writer wins): refused.
    assert stored_stamp("2036-10-03 12:00:00", now=now) is None
    for raw in (None, 3, "", "not a date"):
        assert stored_stamp(raw, now=now) is None


@pytest.mark.parametrize(
    ("emoji", "ok"),
    [
        ("👍", True),
        ("❤️", True),
        ("", False),
        (None, False),
        ("x" * 33, False),
        (" 👍", False),
        ("e\u0301", False),  # decomposed: not NFC
    ],
)
def test_reaction_emoji_ok_is_the_live_rule(emoji, ok):
    assert reaction_emoji_ok(emoji) is ok


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


# ── Payload suites (retired) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "PAYLOAD_SUITE_JSON",
        "PAYLOAD_SUITE_ZLIB",
        "PAYLOAD_SUITE_ZLIB_DICT1",
        "PAYLOAD_SUITE_FIELD",
        "COMPRESSED_PAYLOAD_SUITES",
        "payload_suite_of",
        "zlib_item_plaintext",
        "compressed_item_plaintexts",
        "seal_item_plaintext",
        "open_item_plaintext",
    ],
)
def test_the_compressed_payload_suites_are_gone(name):
    """v_96 (P7) — the compressed item suites rode only the space-keyed
    relay (``/gfs/publish``, the member publish); a channel item is always
    padded JSON, so neither the codec nor its preset dictionary is left."""
    assert not hasattr(space_item, name)
    with pytest.raises(ImportError):
        __import__("socialhome.domain.item_zdict")


def test_an_item_is_padded_json_to_a_bucket():
    body = {"item_type": "post", "inner": {"content": "hi"}}
    sealed = pad_json_object(body)
    assert len(sealed) in ITEM_SIZE_BUCKETS
    assert {k: v for k, v in json.loads(sealed).items() if k != PAD_FIELD} == body


@pytest.mark.parametrize(
    ("emoji", "ok"),
    [
        ("👍", True),
        ("❤️", True),
        ("1️⃣", True),
        ("👨‍👩‍👧", True),
        ("nice", False),
        ("+1", False),
        ("👍 👍", False),
        ("👍\n", False),
        ("", False),
        ("😀" * 33, False),
    ],
)
def test_a_reaction_is_an_emoji_not_free_text(emoji, ok):
    """The status-emoji sanity check, for reactions: no whitespace or
    control characters and at least one non-ASCII code point."""
    from socialhome.domain.space_item import reaction_emoji_ok

    assert reaction_emoji_ok(emoji) is ok


@pytest.mark.parametrize(
    ("emoji", "ok"),
    [("nice", True), ("+1", True), ("👍", True), ("", False), ("x" * 33, False)]
    + [(" x", False), ("e\u0301", False), (None, False)],
)
def test_a_reaction_being_removed_may_be_old_free_text(emoji, ok):
    """``legacy=True`` — a removal, or a catch-up copy of an old reaction:
    the rule from before v_93 (a short NFC string), so a free-text reaction
    made back then can still be taken back everywhere."""
    from socialhome.domain.space_item import reaction_emoji_ok

    assert reaction_emoji_ok(emoji, legacy=True) is ok


def test_the_reaction_and_status_emoji_share_one_rule():
    from socialhome.domain.space_item import reaction_emoji_ok
    from socialhome.domain.user import clean_status_emoji, looks_like_emoji

    for text in ("👍", "1️⃣", "busy", "a b", "👍 👍"):
        try:
            status_ok = clean_status_emoji(text) is not None
        except ValueError:
            status_ok = False
        assert looks_like_emoji(text) is status_ok is reaction_emoji_ok(text), text


def test_a_sixth_reaction_is_a_coded_refusal():
    from socialhome.domain.errors import CodedError
    from socialhome.domain.space_item import TooManyReactionsError

    err = TooManyReactionsError()
    assert isinstance(err, CodedError) and isinstance(err, ValueError)
    assert (err.status, err.code) == (422, "REACTION_LIMIT")
    assert "too many" in str(err).lower()
