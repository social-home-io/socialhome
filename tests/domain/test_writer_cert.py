"""Tests for the WriterCert domain dataclass + wire codec."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.writer_cert import (
    FOLLOWER_CERT_SKEW_S,
    FOLLOWER_CERT_TTL_S,
    FOLLOWER_SCOPES,
    WRITER_ACTION_PROFILE,
    WRITER_ACTION_REACT,
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_FOLLOWER,
    WRITER_SCOPE_FOLLOWER_COMMENT,
    WRITER_SCOPE_FOLLOWER_REACT,
    WRITER_SCOPE_WRITE,
    WRITER_SCOPES,
    WriterCert,
    follower_cert_fresh,
    follower_scope,
    scope_permits,
    strongest_scope,
)


def _cert(**over) -> WriterCert:
    base = {
        "cert_suite": "ed25519",
        "space_id": "sp-1",
        "epoch": 3,
        "instance_pk": "A" * 43,
        "scope": WRITER_SCOPE_WRITE,
        "issued_at": 1_700_000_000,
        "cert_sig": "sig",
    }
    base.update(over)
    return WriterCert(**base)


def test_frozen():
    cert = _cert()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cert.scope = WRITER_SCOPE_COMMENT  # type: ignore[misc]


def test_wire_round_trip():
    cert = _cert()
    wire = cert.to_wire()
    assert wire == {
        "cert_suite": "ed25519",
        "space_id": "sp-1",
        "epoch": 3,
        "instance_pk": "A" * 43,
        "scope": "write",
        "issued_at": 1_700_000_000,
        "cert_sig": "sig",
    }
    assert WriterCert.from_wire(wire) == cert


def test_signing_body_excludes_signature():
    body = _cert().signing_body()
    assert "cert_sig" not in body
    assert body["cert_suite"] == "ed25519"
    assert set(body) == {
        "cert_suite",
        "space_id",
        "epoch",
        "instance_pk",
        "scope",
        "issued_at",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"cert_suite": 1},
        {"space_id": ""},
        {"epoch": "3"},
        {"epoch": True},
        {"epoch": -1},
        {"instance_pk": ""},
        {"scope": "admin"},
        {"issued_at": "now"},
        {"cert_sig": None},
    ],
)
def test_from_wire_rejects_malformed(mutation):
    wire = _cert().to_wire()
    wire.update(mutation)
    with pytest.raises(ValueError):
        WriterCert.from_wire(wire)


def test_from_wire_rejects_missing_field_and_non_dict():
    wire = _cert().to_wire()
    del wire["scope"]
    with pytest.raises(ValueError):
        WriterCert.from_wire(wire)
    with pytest.raises(ValueError):
        WriterCert.from_wire("nope")


def test_scope_permits():
    assert scope_permits(WRITER_SCOPE_WRITE, WRITER_SCOPE_WRITE)
    assert scope_permits(WRITER_SCOPE_WRITE, WRITER_SCOPE_COMMENT)
    assert scope_permits(WRITER_SCOPE_COMMENT, WRITER_SCOPE_COMMENT)
    assert not scope_permits(WRITER_SCOPE_COMMENT, WRITER_SCOPE_WRITE)
    assert not scope_permits("bogus", WRITER_SCOPE_COMMENT)
    assert not scope_permits(WRITER_SCOPE_WRITE, "bogus")


def test_follower_scopes_follow_the_switches():
    """A follower (subscriber-only) household's cert says which of the
    space's switches were on when it was issued — a receiver that holds no
    config (a GFS subscriber) can then refuse what the switch forbids."""
    assert FOLLOWER_SCOPES == frozenset(
        {"follower", "follower_comment", "follower_react"}
    )
    assert follower_scope(comment=True, react=True) == WRITER_SCOPE_FOLLOWER
    assert follower_scope(comment=True, react=False) == WRITER_SCOPE_FOLLOWER_COMMENT
    assert follower_scope(comment=False, react=True) == WRITER_SCOPE_FOLLOWER_REACT
    assert follower_scope(comment=False, react=False) is None
    for scope in FOLLOWER_SCOPES:
        assert not scope_permits(scope, WRITER_SCOPE_WRITE)
        assert scope_permits(scope, WRITER_ACTION_PROFILE)
    assert scope_permits(WRITER_SCOPE_FOLLOWER, WRITER_SCOPE_COMMENT)
    assert scope_permits(WRITER_SCOPE_FOLLOWER, WRITER_ACTION_REACT)
    assert scope_permits(WRITER_SCOPE_FOLLOWER_COMMENT, WRITER_SCOPE_COMMENT)
    assert not scope_permits(WRITER_SCOPE_FOLLOWER_COMMENT, WRITER_ACTION_REACT)
    assert scope_permits(WRITER_SCOPE_FOLLOWER_REACT, WRITER_ACTION_REACT)
    assert not scope_permits(WRITER_SCOPE_FOLLOWER_REACT, WRITER_SCOPE_COMMENT)
    # A member's scopes keep every right they had, and react with them.
    assert scope_permits(WRITER_SCOPE_COMMENT, WRITER_ACTION_REACT)
    assert scope_permits(WRITER_SCOPE_WRITE, WRITER_ACTION_REACT)


def test_a_follower_cert_lives_24_hours():
    """A follower cert is refused once its signed ``issued_at`` is older
    than the TTL, or dated further ahead than the skew allowance; a member's
    cert has no lifetime (it dies with its epoch)."""
    now = 1_800_000_000
    assert FOLLOWER_CERT_TTL_S == 24 * 60 * 60 and FOLLOWER_CERT_SKEW_S == 300
    fresh = _cert(scope=WRITER_SCOPE_FOLLOWER, issued_at=now - FOLLOWER_CERT_TTL_S)
    assert follower_cert_fresh(fresh, now=now)
    old = _cert(scope=WRITER_SCOPE_FOLLOWER, issued_at=now - FOLLOWER_CERT_TTL_S - 1)
    assert not follower_cert_fresh(old, now=now)
    ahead = _cert(scope=WRITER_SCOPE_FOLLOWER, issued_at=now + FOLLOWER_CERT_SKEW_S + 1)
    assert not follower_cert_fresh(ahead, now=now)
    assert follower_cert_fresh(
        _cert(scope=WRITER_SCOPE_FOLLOWER, issued_at=now + FOLLOWER_CERT_SKEW_S),
        now=now,
    )
    assert follower_cert_fresh(_cert(scope=WRITER_SCOPE_WRITE, issued_at=0), now=now)


def test_strongest_scope():
    assert WRITER_SCOPES == frozenset(
        {"write", "comment", "follower", "follower_comment", "follower_react"}
    )
    assert strongest_scope([WRITER_SCOPE_FOLLOWER, WRITER_SCOPE_COMMENT]) == (
        WRITER_SCOPE_COMMENT
    )
    assert strongest_scope([WRITER_SCOPE_FOLLOWER_REACT]) == WRITER_SCOPE_FOLLOWER_REACT
    assert strongest_scope([]) is None
    assert strongest_scope([WRITER_SCOPE_COMMENT]) == WRITER_SCOPE_COMMENT
    assert (
        strongest_scope([WRITER_SCOPE_COMMENT, WRITER_SCOPE_WRITE])
        == WRITER_SCOPE_WRITE
    )
