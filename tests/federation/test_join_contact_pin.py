"""Pinning a global space's join contact from GFS listings (v_84)."""

from __future__ import annotations

import orjson

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.federation.join_contact_pin import (
    candidate_from_listing,
    load_pin,
    merge_pin,
    usable_contact,
)
from socialhome.space_join_contact import sign_join_contact

GFS_A = "https://gfs-a.example"
GFS_B = "https://gfs-b.example"


def _host():
    ident = generate_identity_keypair()
    keywrap = generate_x25519_keypair()
    return {
        "instance_id": derive_instance_id(ident.public_key),
        "identity_pk": ident.public_key.hex(),
        "keywrap_pk": keywrap.public_key.hex(),
        "keywrap_sig": b64url_encode(
            sign_ed25519(ident.private_key, keywrap.public_key)
        ),
    }


def _item(host, space_kp, *, space_id="sp-1", seq=0, **over) -> dict:
    contact = sign_join_contact(
        space_id=space_id, space_seed=space_kp.private_key, **host
    )
    return {
        "space_id": space_id,
        "owning_instance": host["instance_id"],
        "identity_public_key": space_kp.public_key.hex(),
        "authority_rotation_seq": seq,
        "join_contact": contact,
        **over,
    }


def test_a_valid_listing_offers_a_usable_contact():
    host, space = _host(), generate_identity_keypair()
    pin = merge_pin(
        None, candidate_from_listing(_item(host, space), gfs_url=GFS_A), space_id="sp-1"
    )
    contact, gfs = usable_contact(pin)
    assert contact["instance_id"] == host["instance_id"]
    assert gfs == GFS_A


def test_a_contact_that_does_not_hold_is_not_offered():
    host, space = _host(), generate_identity_keypair()
    # Names another household than the listed owner.
    item = _item(host, space, owning_instance="someone-else")
    assert usable_contact(candidate_from_listing(item, gfs_url=GFS_A)) is None
    # A key-wrap key not bound to the identity (signed by the space key, but
    # the binding signature is someone else's).
    other = _host()
    bad = {**host, "keywrap_sig": other["keywrap_sig"]}
    assert (
        usable_contact(candidate_from_listing(_item(bad, space), gfs_url=GFS_A)) is None
    )
    # No authority key at all: nothing to pin.
    assert candidate_from_listing({"space_id": "sp-1"}, gfs_url=GFS_A) is None


def test_another_authority_from_another_gfs_is_a_lasting_conflict():
    host, space, forged = (
        _host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    pin = merge_pin(
        None, candidate_from_listing(_item(host, space), gfs_url=GFS_A), space_id="sp-1"
    )
    pin = merge_pin(
        pin, candidate_from_listing(_item(host, forged), gfs_url=GFS_B), space_id="sp-1"
    )
    assert usable_contact(pin) is None
    assert load_pin(pin)["conflict"] is True
    # The good listing coming back does not re-open it.
    pin = merge_pin(
        pin, candidate_from_listing(_item(host, space), gfs_url=GFS_A), space_id="sp-1"
    )
    assert usable_contact(pin) is None


def test_a_rotation_is_taken_only_from_the_pinning_gfs_with_a_higher_seq():
    host, space, rotated = (
        _host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    first = merge_pin(
        None, candidate_from_listing(_item(host, space), gfs_url=GFS_A), space_id="sp-1"
    )
    # Same GFS, same seq: refused.
    same = merge_pin(
        first,
        candidate_from_listing(_item(host, rotated), gfs_url=GFS_A),
        space_id="sp-1",
    )
    assert load_pin(same)["conflict"] is True
    # Same GFS, higher seq (an owner-certified re-pin there): taken.
    moved = merge_pin(
        first,
        candidate_from_listing(_item(host, rotated, seq=1), gfs_url=GFS_A),
        space_id="sp-1",
    )
    assert load_pin(moved)["authority_pk"] == rotated.public_key.hex()
    assert usable_contact(moved) is not None


def test_the_same_key_refreshes_the_contact_from_any_gfs_but_keeps_the_pin():
    host, space = _host(), generate_identity_keypair()
    first = merge_pin(
        None, candidate_from_listing(_item(host, space), gfs_url=GFS_A), space_id="sp-1"
    )
    no_contact = _item(host, space)
    no_contact.pop("join_contact")
    after = merge_pin(
        first, candidate_from_listing(no_contact, gfs_url=GFS_B), space_id="sp-1"
    )
    assert usable_contact(after) is None  # the owner stopped listing one
    assert load_pin(after)["gfs"] == GFS_A
    again = merge_pin(
        after,
        candidate_from_listing(_item(host, space), gfs_url=GFS_B),
        space_id="sp-1",
    )
    assert usable_contact(again) == (usable_contact(first)[0], GFS_A)


def test_garbage_reads_as_nothing():
    assert load_pin(None) is None
    assert load_pin("not json") is None
    assert load_pin(orjson.dumps([1]).decode()) is None
    assert merge_pin("x", None, space_id="sp") == "x"
