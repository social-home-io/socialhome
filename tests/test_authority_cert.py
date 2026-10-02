"""Unit tests for :mod:`socialhome.authority_cert` (v_44).

The authority cert is the owner household's statement "space ``S``'s
authority key is now ``K`` at epoch ``N``", signed with the owner
household's identity key. Every receiver (member, admin, subscriber, GFS)
verifies it against keys it already holds: ``derive_instance_id(owner_pk)``
must equal the space's owner instance id. These tests pin the binding and
the suite rules.
"""

from __future__ import annotations

import json

import pytest

from socialhome.authority_cert import (
    AUTHORITY_CERT_SUITE_ED25519,
    AUTHORITY_KEY_SUITE_ED25519,
    SUPPORTED_AUTHORITY_CERT_SUITES,
    SUPPORTED_AUTHORITY_KEY_SUITES,
    InvalidAuthorityCert,
    UnsupportedAuthorityCertSuite,
    authority_cert_epoch,
    authority_cert_signing_bytes,
    sign_authority_cert,
    verify_authority_cert,
)
from socialhome.crypto import derive_instance_id, generate_identity_keypair

SPACE = "sp-cert"


@pytest.fixture
def owner():
    kp = generate_identity_keypair()
    return kp, derive_instance_id(kp.public_key)


def _cert(owner, *, epoch: int = 1, authority_pk_hex: str | None = None) -> dict:
    kp, owner_id = owner
    return sign_authority_cert(
        space_id=SPACE,
        owner_instance_id=owner_id,
        owner_seed=kp.private_key,
        owner_pk_hex=kp.public_key.hex(),
        authority_pk_hex=authority_pk_hex
        or generate_identity_keypair().public_key.hex(),
        key_epoch=epoch,
        issued_at="2026-10-02T00:00:00+00:00",
    )


def test_suites_are_tagged_and_supported():
    assert AUTHORITY_CERT_SUITE_ED25519 in SUPPORTED_AUTHORITY_CERT_SUITES
    assert AUTHORITY_KEY_SUITE_ED25519 in SUPPORTED_AUTHORITY_KEY_SUITES


def test_round_trip_verifies(owner):
    _kp, owner_id = owner
    new_pk = generate_identity_keypair().public_key.hex()
    cert = _cert(owner, epoch=3, authority_pk_hex=new_pk)
    assert cert["cert_sig_suite"] == AUTHORITY_CERT_SUITE_ED25519
    assert cert["authority_key_suite"] == AUTHORITY_KEY_SUITE_ED25519
    got = verify_authority_cert(cert, space_id=SPACE, owner_instance_id=owner_id)
    assert got.authority_pk_hex == new_pk
    assert got.key_epoch == 3
    assert got.owner_instance_id == owner_id


def test_cert_names_no_member_or_reason(owner):
    """The cert carries exactly the routing + key facts — nobody revoked,
    no reason. The GFS learns that a rotation happened, nothing more."""
    cert = _cert(owner)
    assert set(cert) == {
        "space_id",
        "owner_instance_id",
        "owner_pk",
        "authority_pk",
        "authority_key_suite",
        "key_epoch",
        "issued_at",
        "cert_sig_suite",
        "cert_sig",
    }


def test_signing_bytes_are_domain_separated_and_canonical(owner):
    cert = _cert(owner)
    body = authority_cert_signing_bytes(cert)
    assert body.startswith(b"space-authority-cert:v1:")
    reordered = dict(reversed(list(cert.items())))
    assert authority_cert_signing_bytes(reordered) == body
    signed = json.loads(body[len(b"space-authority-cert:v1:") :])
    assert "cert_sig" not in signed
    assert signed["cert_sig_suite"] == AUTHORITY_CERT_SUITE_ED25519


@pytest.mark.parametrize(
    "field,value",
    [
        ("authority_pk", "11" * 32),
        ("key_epoch", 99),
        ("space_id", "other-space"),
        ("issued_at", "2030-01-01T00:00:00+00:00"),
    ],
)
def test_any_tampered_field_fails(owner, field, value):
    _kp, owner_id = owner
    cert = _cert(owner)
    cert[field] = value
    with pytest.raises(InvalidAuthorityCert):
        verify_authority_cert(
            cert,
            space_id=cert["space_id"] if field == "space_id" else SPACE,
            owner_instance_id=owner_id,
        )


def test_wrong_space_is_rejected(owner):
    _kp, owner_id = owner
    with pytest.raises(InvalidAuthorityCert):
        verify_authority_cert(_cert(owner), space_id="sp-x", owner_instance_id=owner_id)


def test_wrong_owner_instance_is_rejected(owner):
    with pytest.raises(InvalidAuthorityCert):
        verify_authority_cert(_cert(owner), space_id=SPACE, owner_instance_id="someone")


def test_owner_pk_must_derive_to_the_owner_id(owner):
    """A cert signed by ANOTHER household's key (e.g. a demoted admin's)
    that merely claims the owner's instance id fails the derive binding."""
    _kp, owner_id = owner
    evil = generate_identity_keypair()
    cert = sign_authority_cert(
        space_id=SPACE,
        owner_instance_id=owner_id,
        owner_seed=evil.private_key,
        owner_pk_hex=evil.public_key.hex(),
        authority_pk_hex="22" * 32,
        key_epoch=5,
    )
    with pytest.raises(InvalidAuthorityCert):
        verify_authority_cert(cert, space_id=SPACE, owner_instance_id=owner_id)


def test_signature_by_old_space_key_is_rejected(owner):
    """Signed with something other than the owner seed (e.g. the old
    space authority key K1) but naming the real owner pk → bad sig."""
    kp, owner_id = owner
    k1 = generate_identity_keypair()
    cert = sign_authority_cert(
        space_id=SPACE,
        owner_instance_id=owner_id,
        owner_seed=k1.private_key,
        owner_pk_hex=kp.public_key.hex(),
        authority_pk_hex="33" * 32,
        key_epoch=2,
    )
    with pytest.raises(InvalidAuthorityCert):
        verify_authority_cert(cert, space_id=SPACE, owner_instance_id=owner_id)


def test_known_owner_pk_must_match(owner):
    _kp, owner_id = owner
    with pytest.raises(InvalidAuthorityCert):
        verify_authority_cert(
            _cert(owner),
            space_id=SPACE,
            owner_instance_id=owner_id,
            known_owner_pk_hex="44" * 32,
        )


def test_known_owner_pk_matching_passes(owner):
    kp, owner_id = owner
    got = verify_authority_cert(
        _cert(owner),
        space_id=SPACE,
        owner_instance_id=owner_id,
        known_owner_pk_hex=kp.public_key.hex(),
    )
    assert got.owner_pk_hex == kp.public_key.hex()


@pytest.mark.parametrize("field", ["cert_sig_suite", "authority_key_suite"])
def test_unknown_suite_raises_no_fallback(owner, field):
    _kp, owner_id = owner
    cert = _cert(owner)
    cert[field] = "ed25519+mldsa65-future"
    with pytest.raises(UnsupportedAuthorityCertSuite):
        verify_authority_cert(cert, space_id=SPACE, owner_instance_id=owner_id)


@pytest.mark.parametrize("field", ["cert_sig_suite", "authority_key_suite"])
def test_missing_suite_raises_no_default(owner, field):
    _kp, owner_id = owner
    cert = _cert(owner)
    del cert[field]
    with pytest.raises(UnsupportedAuthorityCertSuite):
        verify_authority_cert(cert, space_id=SPACE, owner_instance_id=owner_id)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.__setitem__("authority_pk", "ab" * 16),
        lambda c: c.__setitem__("authority_pk", "zz" * 32),
        lambda c: c.__setitem__("key_epoch", 0),
        lambda c: c.__setitem__("key_epoch", -1),
        lambda c: c.__setitem__("key_epoch", True),
        lambda c: c.__setitem__("key_epoch", "1"),
        lambda c: c.__setitem__("cert_sig", "!!"),
        lambda c: c.__setitem__("owner_pk", "00"),
        lambda c: c.pop("cert_sig"),
    ],
)
def test_malformed_fields_are_rejected(owner, mutate):
    _kp, owner_id = owner
    cert = _cert(owner)
    mutate(cert)
    with pytest.raises(InvalidAuthorityCert):
        verify_authority_cert(cert, space_id=SPACE, owner_instance_id=owner_id)


@pytest.mark.parametrize("value", [None, "cert", 5, ["x"]])
def test_non_dict_is_rejected(value):
    with pytest.raises(InvalidAuthorityCert):
        verify_authority_cert(value, space_id=SPACE, owner_instance_id="x")


def test_sign_rejects_bad_epoch(owner):
    kp, owner_id = owner
    with pytest.raises(ValueError):
        sign_authority_cert(
            space_id=SPACE,
            owner_instance_id=owner_id,
            owner_seed=kp.private_key,
            owner_pk_hex=kp.public_key.hex(),
            authority_pk_hex="55" * 32,
            key_epoch=0,
        )


def test_issued_at_defaults_to_now(owner):
    kp, owner_id = owner
    cert = sign_authority_cert(
        space_id=SPACE,
        owner_instance_id=owner_id,
        owner_seed=kp.private_key,
        owner_pk_hex=kp.public_key.hex(),
        authority_pk_hex="66" * 32,
        key_epoch=1,
    )
    assert cert["issued_at"].endswith("+00:00")


@pytest.mark.parametrize(
    "cert,expected",
    [
        (None, 0),
        ({}, 0),
        ({"key_epoch": 4}, 4),
        ({"key_epoch": "4"}, 0),
        ({"key_epoch": True}, 0),
        ({"key_epoch": -2}, 0),
        ("nope", 0),
    ],
)
def test_authority_cert_epoch(cert, expected):
    assert authority_cert_epoch(cert) == expected


def test_epoch_above_int64_is_refused(owner):
    from socialhome.authority_cert import MAX_AUTHORITY_KEY_EPOCH

    kp, owner_id = owner
    with pytest.raises(ValueError):
        _cert(owner, epoch=MAX_AUTHORITY_KEY_EPOCH + 1)
    cert = _cert(owner, epoch=MAX_AUTHORITY_KEY_EPOCH)
    assert (
        verify_authority_cert(
            cert, space_id=SPACE, owner_instance_id=owner_id
        ).key_epoch
        == MAX_AUTHORITY_KEY_EPOCH
    )
    cert["key_epoch"] = MAX_AUTHORITY_KEY_EPOCH + 1
    with pytest.raises(InvalidAuthorityCert):
        verify_authority_cert(cert, space_id=SPACE, owner_instance_id=owner_id)
    assert authority_cert_epoch({"key_epoch": MAX_AUTHORITY_KEY_EPOCH + 1}) == 0
