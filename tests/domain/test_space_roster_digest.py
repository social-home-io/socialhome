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
from socialhome.domain.writer_cert import MAX_WRITER_CERT_EPOCH


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


def test_the_wire_is_the_digest_and_the_cert_epoch():
    """Round 4: the host's one rule compares the seats' digest and the
    writer-cert epoch, nothing else — no other field rides the BEGIN."""
    wire = roster_digest_wire("a" * 64, 7)
    assert set(wire) == {"suite", "digest", "cert_epoch"}
    assert parse_roster_digest(wire) == RosterDigest(digest="a" * 64, cert_epoch=7)
    assert parse_roster_digest(roster_digest_wire("b" * 64, None)) == RosterDigest(
        digest="b" * 64, cert_epoch=None
    )


@pytest.mark.parametrize("cert_epoch", [-1, "1", True, 1.5, MAX_WRITER_CERT_EPOCH + 1])
def test_a_cert_epoch_out_of_range_is_malformed(cert_epoch):
    wire = roster_digest_wire("a" * 64, None)
    wire["cert_epoch"] = cert_epoch
    assert parse_roster_digest(wire) is None


@pytest.mark.parametrize("suite", [[], {}, ["rd1-sha256"], 7, None])
def test_a_suite_that_is_not_a_string_is_malformed_not_a_crash(suite):
    """Round 4: ``{"suite": []}`` used to raise TypeError (unhashable) —
    an ERROR stack trace per BEGIN. A non-string suite is malformed."""
    assert parse_roster_digest({"suite": suite, "digest": "a" * 64}) is None


def test_the_wanted_flags_ride_only_when_set():
    """On demand: ``cert_wanted`` / ``key_wanted`` ride the BEGIN only when
    true; anything but a bool there is malformed."""
    assert set(roster_digest_wire("a" * 64, None)) == {"suite", "digest", "cert_epoch"}
    wire = roster_digest_wire("a" * 64, 3, cert_wanted=True, key_wanted=True)
    assert parse_roster_digest(wire) == RosterDigest(
        digest="a" * 64, cert_epoch=3, cert_wanted=True, key_wanted=True
    )
    for bad in ("yes", 1, None):
        assert parse_roster_digest({**wire, "cert_wanted": bad}) is None
