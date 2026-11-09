"""Shapes of per-space mailbox entries and stored rows (v_88)."""

from __future__ import annotations

import pytest

from socialhome.crypto import b64url_encode
from socialhome.domain.gfs_mailbox import derive_mailbox_id
from socialhome.domain.space_mailbox import (
    ENTRY_FIELDS,
    InvalidMailboxEntry,
    OwnMailbox,
    entry_signed_bytes,
    own_to_wire,
    parse_entry,
    parse_own,
)

_SIGN_PK = b"\x07" * 32


def _wire(**over):
    base = {
        "space": "sp-1",
        "instance_id": "a" * 32,
        "mailbox_id": derive_mailbox_id(_SIGN_PK),
        "kem_pk": b64url_encode(b"\x02" * 32),
        "kem_suite": "x25519",
        "sign_pk": b64url_encode(_SIGN_PK),
        "gfs": "https://gfs.example.org",
        "gen": 0,
        "proto_version": 88,
        "keywrap_pk": "ab" * 32,
        "keywrap_sig": "c2ln",
        "sig_suite": "ed25519",
        "sig": "c2ln",
    }
    base.update(over)
    return base


def test_parse_round_trips():
    entry = parse_entry(_wire())
    assert entry.to_wire() == _wire()
    assert set(entry.signed_fields()) == set(ENTRY_FIELDS)


@pytest.mark.parametrize(
    "over",
    [
        {"mailbox_id": "b" * 32},
        {"gen": -1},
        {"gen": True},
        {"gfs": "ftp://x"},
        {"kem_pk": b64url_encode(b"\x02" * 31)},
        {"instance_id": "NOTANID"},
        {"space": "a|b"},
        {"keywrap_pk": "zz"},
    ],
)
def test_malformed_fields_are_refused(over):
    with pytest.raises(InvalidMailboxEntry):
        parse_entry(_wire(**over))


def test_extra_or_missing_fields_are_refused():
    with pytest.raises(InvalidMailboxEntry):
        parse_entry({**_wire(), "extra": 1})
    wire = _wire()
    del wire["gfs"]
    with pytest.raises(InvalidMailboxEntry):
        parse_entry(wire)
    with pytest.raises(InvalidMailboxEntry):
        parse_entry("not a dict")


def test_mailbox_id_equal_to_instance_id_is_refused():
    mid = derive_mailbox_id(_SIGN_PK)
    with pytest.raises(InvalidMailboxEntry):
        parse_entry(_wire(instance_id=mid))


def test_signed_bytes_are_domain_separated_and_exclude_sig():
    a = entry_signed_bytes(_wire())
    assert a == entry_signed_bytes(_wire(sig="b3RoZXI"))
    assert a.startswith(b"sh-mbx-entry:v1\n")
    assert entry_signed_bytes(_wire(gen=1)) != a


def test_own_round_trip_and_bad_values():
    own = OwnMailbox(gen=5, gfs_instance_id="g" * 32, gfs_url="https://g")
    assert parse_own(own_to_wire(own)) == own
    assert parse_own(None) is None
    assert parse_own({"gen": "1", "gfs_id": "g", "gfs": "u"}) is None
    assert parse_own({"gen": 1, "gfs_id": "", "gfs": "u"}) is None
