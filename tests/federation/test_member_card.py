"""Member key cards: signed by the household, self-certifying (v_89)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.member_card import (
    InvalidMemberCard,
    UnsupportedMemberCardSuite,
)
from socialhome.federation.gfs_mailbox import derive_mailbox_keys, new_mailbox_root
from socialhome.federation.member_card import build_card, verify_card


def _household():
    ident = generate_identity_keypair()
    kw = generate_x25519_keypair()
    return {
        "seed": ident.private_key,
        "pk": ident.public_key,
        "id": derive_instance_id(ident.public_key),
        "kw_pub": kw.public_key,
        "kw_sig": b64url_encode(sign_ed25519(ident.private_key, kw.public_key)),
    }


def _card(h, *, mailbox=True, rev=5, kw=None, gfs="https://gfs.example.org"):
    keys = derive_mailbox_keys(new_mailbox_root(), "g" * 32, 3)
    kw = kw or h
    return keys, build_card(
        instance_id=h["id"],
        identity_seed=h["seed"],
        identity_pk=h["pk"],
        keywrap_pk=kw["kw_pub"],
        keywrap_sig=kw["kw_sig"],
        proto_version=89,
        rev=rev,
        mailbox=(keys, gfs) if mailbox else None,
    )


def test_round_trip_verifies_with_a_mailbox():
    h = _household()
    keys, card = _card(h)
    got = verify_card(card.to_wire(), expected_instance_id=h["id"])
    assert got == card
    assert got.mailbox is not None
    assert got.mailbox.mailbox_id == keys.mailbox_id
    assert got.mailbox.kem_pk == keys.kem_public
    assert got.rev == 5 and got.proto_version == 89


def test_a_card_without_a_mailbox_verifies():
    h = _household()
    _keys, card = _card(h, mailbox=False)
    got = verify_card(card.to_wire())
    assert got.mailbox is None and got.instance_id == h["id"]


def test_a_card_carries_no_space():
    """One card serves every space: nothing in it names one."""
    h = _household()
    _keys, card = _card(h)
    assert "space" not in card.to_wire()
    assert "space" not in card.to_wire()["mailbox"]


def test_a_host_cannot_alter_a_card():
    """Any field changed by whoever passes the card on breaks it."""
    h = _household()
    _keys, card = _card(h)
    for field, value in (
        ("rev", 6),
        ("proto_version", 90),
        ("keywrap_pk", "0" * 64),
    ):
        wire = card.to_wire()
        wire[field] = value
        with pytest.raises(InvalidMemberCard):
            verify_card(wire)
    wire = card.to_wire()
    wire["mailbox"] = {**wire["mailbox"], "gfs": "https://evil.example.org"}
    with pytest.raises(InvalidMemberCard):
        verify_card(wire)
    wire = card.to_wire()
    wire["mailbox"] = None
    with pytest.raises(InvalidMemberCard):
        verify_card(wire)


def test_a_card_signed_by_another_household_is_refused():
    """A host minting a card for someone else: the id must derive from the
    signing key, and a key that derives to another id is refused."""
    h, other = _household(), _household()
    _keys, card = _card(other)
    wire = card.to_wire()
    wire["instance_id"] = h["id"]
    with pytest.raises(InvalidMemberCard):
        verify_card(wire)
    with pytest.raises(InvalidMemberCard, match="another household"):
        verify_card(card.to_wire(), expected_instance_id=h["id"])


def test_an_unbound_keywrap_key_is_refused():
    h, other = _household(), _household()
    _keys, card = _card(h, kw=other)
    with pytest.raises(InvalidMemberCard, match="key-wrap"):
        verify_card(card.to_wire())


def test_unknown_suites_are_refused_never_defaulted():
    h = _household()
    _keys, card = _card(h)
    wire = card.to_wire()
    wire["sig_suite"] = "ed25519+mldsa65"
    with pytest.raises(UnsupportedMemberCardSuite):
        verify_card(wire)
    wire = card.to_wire()
    wire["mailbox"] = {**wire["mailbox"], "kem_suite": "x25519+mlkem768"}
    with pytest.raises(UnsupportedMemberCardSuite):
        verify_card(wire)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: w.pop("rev"),
        lambda w: w.update(extra=1),
        lambda w: w.update(rev=-1),
        lambda w: w.update(rev=True),
        lambda w: w.update(identity_pk="zz"),
        lambda w: w["mailbox"].update(mailbox_id="a" * 32),
        lambda w: w["mailbox"].update(gfs="ftp://x"),
        lambda w: w["mailbox"].update(kem_pk="AAAA"),
        lambda w: w["mailbox"].pop("gen"),
    ],
)
def test_malformed_cards_are_refused(mutate):
    h = _household()
    _keys, card = _card(h)
    wire = card.to_wire()
    wire["mailbox"] = dict(wire["mailbox"])
    mutate(wire)
    with pytest.raises(InvalidMemberCard):
        verify_card(wire)


def test_a_non_object_is_refused():
    with pytest.raises(InvalidMemberCard):
        verify_card(["not", "a", "card"])
