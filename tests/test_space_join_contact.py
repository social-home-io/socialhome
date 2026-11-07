"""A global space's join contact, signed by the space authority key (v_84)."""

from __future__ import annotations

from socialhome.crypto import generate_identity_keypair
from socialhome.space_join_contact import (
    parse_join_contact,
    sign_join_contact,
    verify_join_contact,
)


def _contact(seed: bytes, space_id: str = "sp-1") -> dict:
    return sign_join_contact(
        space_id=space_id,
        instance_id="host",
        identity_pk="aa" * 32,
        keywrap_pk="bb" * 32,
        keywrap_sig="sig",
        space_seed=seed,
    )


def test_a_contact_verifies_under_its_space_key_only():
    space = generate_identity_keypair()
    other = generate_identity_keypair()
    c = _contact(space.private_key)
    assert verify_join_contact(
        c, space_id="sp-1", space_public_key_hex=space.public_key.hex()
    )
    # Another space's key, another space id: refused.
    assert not verify_join_contact(
        c, space_id="sp-1", space_public_key_hex=other.public_key.hex()
    )
    assert not verify_join_contact(
        c, space_id="sp-2", space_public_key_hex=space.public_key.hex()
    )


def test_a_swapped_key_breaks_the_signature():
    space = generate_identity_keypair()
    c = {**_contact(space.private_key), "keywrap_pk": "cc" * 32}
    assert not verify_join_contact(
        c, space_id="sp-1", space_public_key_hex=space.public_key.hex()
    )


def test_unknown_suites_and_shapes_are_refused():
    space = generate_identity_keypair()
    good = _contact(space.private_key)
    pk = space.public_key.hex()
    assert not verify_join_contact(
        {**good, "authority_sig_suite": "ed448"},
        space_id="sp-1",
        space_public_key_hex=pk,
    )
    assert parse_join_contact({**good, "kem_suite": "x448"}) is None
    assert parse_join_contact({**good, "extra": "x"}) is None
    assert parse_join_contact({**good, "instance_id": 1}) is None
    assert parse_join_contact("nope") is None
    assert not verify_join_contact(good, space_id="sp-1", space_public_key_hex="zz")
