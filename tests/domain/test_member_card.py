"""The member card shape (v_89): parsing and suite checks only — the
signature is :mod:`socialhome.federation.member_card`'s job."""

from __future__ import annotations

import pytest

from socialhome.crypto import b64url_encode, derive_instance_id, ed25519_public_key
from socialhome.domain.gfs_mailbox import derive_mailbox_id
from socialhome.domain.member_card import (
    CARD_FIELDS,
    CARD_SIGNED_CONTEXT,
    InvalidMemberCard,
    UnsupportedMemberCardSuite,
    card_signed_bytes,
    parse_card,
)

_SIGN_PK = ed25519_public_key(bytes([7]) * 32)
_ID = derive_instance_id(ed25519_public_key(bytes([3]) * 32))


def _wire(**over):
    wire = {
        "instance_id": _ID,
        "identity_pk": "ab" * 32,
        "keywrap_pk": "cd" * 32,
        "keywrap_sig": "c2ln",
        "proto_version": 89,
        "rev": 1,
        "mailbox": {
            "mailbox_id": derive_mailbox_id(_SIGN_PK),
            "kem_pk": b64url_encode(b"\x01" * 32),
            "kem_suite": "x25519",
            "sign_pk": b64url_encode(_SIGN_PK),
            "gfs": "https://gfs.example.org",
            "gen": 4,
        },
        "sig_suite": "ed25519",
        "sig": "c2ln",
    }
    wire.update(over)
    return wire


def test_a_well_formed_card_round_trips_through_its_wire_shape():
    card = parse_card(_wire())
    assert card.to_wire() == _wire()
    assert card.mailbox is not None and card.mailbox.gen == 4
    assert parse_card(_wire(mailbox=None)).mailbox is None


def test_the_signed_bytes_are_domain_separated_and_exclude_the_sig():
    fields = parse_card(_wire()).signed_fields()
    signed = card_signed_bytes(fields)
    assert signed.startswith(CARD_SIGNED_CONTEXT)
    assert b'"sig"' not in signed
    assert set(fields) == set(CARD_FIELDS)


def test_unknown_suites_raise_the_suite_error():
    with pytest.raises(UnsupportedMemberCardSuite):
        parse_card(_wire(sig_suite="ed25519+mldsa65"))
    mailbox = {**_wire()["mailbox"], "kem_suite": "x25519+mlkem768"}
    with pytest.raises(UnsupportedMemberCardSuite):
        parse_card(_wire(mailbox=mailbox))


def test_a_mailbox_id_must_be_its_claim_keys_and_never_the_instance_id():
    mailbox = {**_wire()["mailbox"], "mailbox_id": "a" * 32}
    with pytest.raises(InvalidMemberCard, match="claim key"):
        parse_card(_wire(mailbox=mailbox))
