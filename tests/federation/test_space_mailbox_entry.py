"""Signed per-space mailbox directory entries (GFS redesign PR 2, v_88)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.space_mailbox import InvalidMailboxEntry
from socialhome.federation.gfs_mailbox import derive_mailbox_keys, new_mailbox_root
from socialhome.federation.space_mailbox_entry import build_entry, verify_entry


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


def _entry(h, *, space="space-1", gen=3, kw=None):
    keys = derive_mailbox_keys(new_mailbox_root(), "g" * 32, space, gen)
    kw = kw or h
    return keys, build_entry(
        keys,
        instance_id=h["id"],
        identity_seed=h["seed"],
        gfs_url="https://gfs.example.org",
        proto_version=88,
        keywrap_pk=kw["kw_pub"],
        keywrap_sig=kw["kw_sig"],
    )


def _verify(wire, h, *, space="space-1", instance_id=None):
    return verify_entry(
        wire,
        identity_pk_hex=h["pk"].hex(),
        expected_instance_id=instance_id or h["id"],
        expected_space_id=space,
    )


def test_round_trip_verifies():
    h = _household()
    keys, entry = _entry(h)
    got = _verify(entry.to_wire(), h)
    assert got == entry
    assert got.mailbox_id == keys.mailbox_id
    assert got.kem_pk == keys.kem_public
    assert got.gen == 3 and got.proto_version == 88
    assert got.mailbox_id != h["id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("gen", 4),
        ("gfs", "https://evil.example.org"),
        ("kem_pk", b64url_encode(b"\x01" * 32)),
        ("proto_version", 99),
    ],
)
def test_any_tampered_field_fails(field, value):
    h = _household()
    _, entry = _entry(h)
    with pytest.raises(InvalidMailboxEntry):
        _verify({**entry.to_wire(), field: value}, h)


def test_wrong_space_or_household_or_key_fails():
    h, other = _household(), _household()
    _, entry = _entry(h)
    wire = entry.to_wire()
    with pytest.raises(InvalidMailboxEntry, match="another space"):
        _verify(wire, h, space="space-2")
    with pytest.raises(InvalidMailboxEntry):
        _verify(wire, other)
    with pytest.raises(InvalidMailboxEntry, match="does not match"):
        _verify(wire, other, instance_id=h["id"])


def test_unbound_keywrap_key_fails():
    h, other = _household(), _household()
    _, entry = _entry(h, kw=other)
    with pytest.raises(InvalidMailboxEntry, match="key-wrap"):
        _verify(entry.to_wire(), h)


@pytest.mark.parametrize(
    "field,value",
    [("sig_suite", "ed25519+mldsa65"), ("kem_suite", "x25519+mlkem768")],
)
def test_unknown_suite_is_refused_never_defaulted(field, value):
    h = _household()
    _, entry = _entry(h)
    with pytest.raises(InvalidMailboxEntry, match="unsupported"):
        _verify({**entry.to_wire(), field: value}, h)
