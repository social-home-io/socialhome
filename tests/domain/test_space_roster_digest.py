"""The roster digest of a member household's sync BEGIN."""

from __future__ import annotations

import pytest

from socialhome.domain.space_roster_digest import (
    ROSTER_DIGEST_SUITE_SHA256,
    RosterDigest,
    UnsupportedRosterDigestSuite,
    parse_roster_digest,
    roster_digest,
    roster_digest_wire,
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
