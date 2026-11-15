"""The roster digest of a member household's sync BEGIN."""

from __future__ import annotations

import pytest

from socialhome.domain.space_roster_digest import (
    ROSTER_DIGEST_SUITE_SHA256,
    RosterDigest,
    UnsupportedRosterDigestSuite,
    digest_of_roster_seats,
    parse_roster_digest,
    roster_digest,
    roster_digest_wire,
    roster_seats,
)


def test_order_and_duplicates_do_not_matter():
    a = [("h", "u1", "member"), ("x", "u2", "admin")]
    assert roster_digest(a) == roster_digest(list(reversed(a)) + a[:1])


def test_the_hosts_owner_hashes_as_the_member_its_mirror_holds():
    """Every receiver mirrors the host's ``owner`` as ``member``."""
    assert roster_digest([("h", "u", "owner")]) == roster_digest([("h", "u", "member")])


@pytest.mark.parametrize(
    "other",
    [
        [("h", "u1", "admin")],  # a role change
        [("h", "u1", "member"), ("x", "u2", "member")],  # a join
        [],  # a removal
        [("y", "u1", "member")],  # another household
    ],
)
def test_any_seat_change_changes_the_digest(other):
    assert roster_digest([("h", "u1", "member")]) != roster_digest(other)


def test_the_wire_round_trips():
    wire = roster_digest_wire("a" * 64, 7)
    assert wire["suite"] == ROSTER_DIGEST_SUITE_SHA256
    assert parse_roster_digest(wire) == RosterDigest(digest="a" * 64, cert_epoch=7)
    assert parse_roster_digest(roster_digest_wire("b" * 64, None)) == RosterDigest(
        digest="b" * 64, cert_epoch=None
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "x",
        {"suite": ROSTER_DIGEST_SUITE_SHA256, "digest": "zz", "cert_epoch": 1},
        {"suite": ROSTER_DIGEST_SUITE_SHA256, "digest": "A" * 64},
        {"suite": ROSTER_DIGEST_SUITE_SHA256, "digest": "a" * 64, "cert_epoch": "1"},
        {"suite": ROSTER_DIGEST_SUITE_SHA256, "digest": "a" * 64, "cert_epoch": True},
    ],
)
def test_a_malformed_digest_is_none(raw):
    assert parse_roster_digest(raw) is None


def test_an_unknown_suite_is_refused():
    with pytest.raises(UnsupportedRosterDigestSuite):
        parse_roster_digest({"suite": "rd2", "digest": "a" * 64})
    with pytest.raises(UnsupportedRosterDigestSuite):
        parse_roster_digest({"digest": "a" * 64})


def test_a_separator_inside_an_id_never_collides():
    """M1: the seats are hashed as a canonical JSON list, not joined text —
    an id that contains a separator cannot forge another seat list."""
    assert roster_digest([("a\x1fb", "c", "member")]) != roster_digest(
        [("a", "b\x1fc", "member")]
    )
    assert roster_digest([("a", "b", "member"), ("c", "d", "member")]) != (
        roster_digest([("a", "b\nc", "member"), ("", "d", "member")])
    )


def test_the_sorted_seats_hash_like_the_raw_ones_without_one_household():
    """The host caches the sorted, mirrored seats once and leaves out the
    requester's own household when it hashes."""
    raw = [("x", "u2", "owner"), ("h", "u1", "member"), ("x", "u2", "owner")]
    seats = roster_seats(raw)
    assert seats == (("h", "u1", "member"), ("x", "u2", "member"))
    assert digest_of_roster_seats(seats) == roster_digest(raw)
    assert digest_of_roster_seats(seats, exclude_instance="x") == roster_digest(
        [("h", "u1", "member")]
    )


def test_the_wire_carries_the_applied_roster_version_and_key_epoch():
    """S2 / N6: the member also says which roster version it has applied
    and which content-key epoch it holds, so the host can tell "behind
    while gossip is in flight" and "key not caught up yet" apart."""
    wire = roster_digest_wire("a" * 64, 7, roster_version=12, key_epoch=7)
    assert wire["roster_version"] == 12 and wire["key_epoch"] == 7
    assert parse_roster_digest(wire) == RosterDigest(
        digest="a" * 64, cert_epoch=7, roster_version=12, key_epoch=7
    )
    bare = roster_digest_wire("b" * 64, None)
    assert parse_roster_digest(bare) == RosterDigest(
        digest="b" * 64, cert_epoch=None, roster_version=None, key_epoch=None
    )


@pytest.mark.parametrize(
    "field", [("roster_version", -1), ("roster_version", "1"), ("key_epoch", True)]
)
def test_a_malformed_version_or_epoch_is_none(field):
    wire = roster_digest_wire("a" * 64, None)
    wire[field[0]] = field[1]
    assert parse_roster_digest(wire) is None
