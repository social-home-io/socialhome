"""Tests for :mod:`socialhome.gfs_client_status` — a pending household asks
its GFS whether the operator approved it.

The household signs its question for one GFS (only the key holder may learn
its own status there — no registration oracle, and the question can't be
replayed at another GFS); the GFS signs its answer with its identity key,
bound to the household's fresh nonce, instance id and question signature, so
nobody can fake an approval, replay an old answer or swap the question.
"""

from __future__ import annotations

import pytest

from socialhome.capabilities_sig import CAPS_SIGNING_PREFIX
from socialhome.crypto import b64url_encode, generate_identity_keypair
from socialhome.gfs_client_status import (
    CLIENT_STATUS_REQUEST_PREFIX,
    CLIENT_STATUS_SIG_SUITE_ED25519,
    CLIENT_STATUS_SIGNING_PREFIX,
    CLIENT_STATUSES,
    SUPPORTED_CLIENT_STATUS_SIG_SUITES,
    UnsupportedClientStatusSigSuite,
    client_status_signing_bytes,
    parse_status_request,
    sign_client_status,
    sign_status_request,
    status_request_signing_bytes,
    verify_client_status,
    verify_status_request,
)
from socialhome.gfs_proof import PROOF_SIGNING_PREFIX, new_proof_nonce

HOME = "home-alpha"
GFS = generate_identity_keypair()
GFS_PK = GFS.public_key.hex()
Q_SIG = "question-sig"


def test_constants():
    assert CLIENT_STATUS_SIG_SUITE_ED25519 == "ed25519"
    assert SUPPORTED_CLIENT_STATUS_SIG_SUITES == frozenset({"ed25519"})
    assert CLIENT_STATUSES == frozenset({"active", "pending", "banned", "unknown"})
    assert issubclass(UnsupportedClientStatusSigSuite, ValueError)


def test_prefixes_are_domain_separated():
    """From each other and from everything else the GFS key signs: the
    possession proof and the capability block. None is a prefix of another."""
    prefixes = [
        CLIENT_STATUS_REQUEST_PREFIX,
        CLIENT_STATUS_SIGNING_PREFIX,
        PROOF_SIGNING_PREFIX,
        CAPS_SIGNING_PREFIX,
    ]
    for a in prefixes:
        for b in prefixes:
            if a is not b:
                assert not a.startswith(b)


def test_request_round_trip():
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    block = sign_status_request(kp.private_key, GFS_PK, HOME, nonce)
    assert set(block) == {"instance_id", "sig", "sig_suite"}
    parsed = parse_status_request(block)
    assert parsed == (HOME, block["sig"], "ed25519")
    pk = kp.public_key.hex()
    assert verify_status_request(pk, GFS_PK, HOME, nonce, block["sig"], "ed25519")


def test_request_bound_to_gfs_nonce_and_instance():
    """A question can't be replayed at another GFS, with another nonce, or
    for another household."""
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    sig = sign_status_request(kp.private_key, GFS_PK, HOME, nonce)["sig"]
    pk = kp.public_key.hex()
    other_gfs = generate_identity_keypair().public_key.hex()
    assert not verify_status_request(pk, other_gfs, HOME, nonce, sig, "ed25519")
    assert not verify_status_request(
        pk, GFS_PK, HOME, new_proof_nonce(), sig, "ed25519"
    )
    assert not verify_status_request(pk, GFS_PK, "home-beta", nonce, sig, "ed25519")
    other = generate_identity_keypair().public_key.hex()
    assert not verify_status_request(other, GFS_PK, HOME, nonce, sig, "ed25519")


def test_request_unknown_suite_raises():
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    sig = sign_status_request(kp.private_key, GFS_PK, HOME, nonce)["sig"]
    with pytest.raises(UnsupportedClientStatusSigSuite):
        verify_status_request(kp.public_key.hex(), GFS_PK, HOME, nonce, sig, "rsa")


@pytest.mark.parametrize(
    "value",
    [
        None,
        "x",
        [],
        {},
        {"instance_id": HOME, "sig": "s"},
        {"instance_id": "", "sig": "s", "sig_suite": "ed25519"},
        {"instance_id": HOME, "sig": "", "sig_suite": "ed25519"},
        {"instance_id": 5, "sig": "s", "sig_suite": "ed25519"},
        {"instance_id": "x" * 257, "sig": "s", "sig_suite": "ed25519"},
        {"instance_id": HOME, "sig": "s" * 200, "sig_suite": "ed25519"},
        {"instance_id": HOME, "sig": "s", "sig_suite": "x" * 65},
    ],
)
def test_parse_request_rejects_malformed(value):
    assert parse_status_request(value) is None


def _answer(status: str, *, nonce: str, key=GFS, request_sig: str = Q_SIG):
    return sign_client_status(key.private_key, HOME, nonce, request_sig, status)


def test_status_round_trip_each_value():
    nonce = new_proof_nonce()
    for status in sorted(CLIENT_STATUSES):
        sig, suite = _answer(status, nonce=nonce)
        assert suite == "ed25519"
        assert verify_client_status(GFS_PK, HOME, nonce, Q_SIG, status, sig, suite)


def test_status_cannot_be_relabelled():
    """A signed ``pending`` (or ``unknown``) never verifies as ``active``."""
    nonce = new_proof_nonce()
    sig, suite = _answer("pending", nonce=nonce)
    assert not verify_client_status(GFS_PK, HOME, nonce, Q_SIG, "active", sig, suite)


def test_status_bound_to_nonce_instance_key_and_question():
    """An old answer can't be replayed, another household's answer can't be
    reused, another key's answer is refused — and an answer to a DIFFERENT
    question (one someone on the path swapped in to get ``unknown``) does
    not verify for the household's own question."""
    nonce = new_proof_nonce()
    sig, suite = _answer("unknown", nonce=nonce, request_sig="swapped")
    assert not verify_client_status(GFS_PK, HOME, nonce, Q_SIG, "unknown", sig, suite)
    sig, suite = _answer("active", nonce=nonce)
    assert not verify_client_status(
        GFS_PK, HOME, new_proof_nonce(), Q_SIG, "active", sig, suite
    )
    assert not verify_client_status(
        GFS_PK, "home-beta", nonce, Q_SIG, "active", sig, suite
    )
    forged, _ = _answer("active", nonce=nonce, key=generate_identity_keypair())
    assert not verify_client_status(GFS_PK, HOME, nonce, Q_SIG, "active", forged, suite)


def test_status_verify_never_raises_on_garbage():
    nonce = new_proof_nonce()
    garbage = b64url_encode(b"x" * 64)
    assert not verify_client_status(
        GFS_PK, HOME, nonce, Q_SIG, "active", "!!", "ed25519"
    )
    assert not verify_client_status(
        "zz", HOME, nonce, Q_SIG, "active", garbage, "ed25519"
    )
    assert not verify_client_status(GFS_PK, HOME, nonce, Q_SIG, "active", "", "ed25519")
    assert not verify_client_status(
        GFS_PK, HOME, nonce, Q_SIG, "approved", garbage, "ed25519"
    )


def test_status_unknown_suite_raises():
    nonce = new_proof_nonce()
    sig, _ = _answer("active", nonce=nonce)
    with pytest.raises(UnsupportedClientStatusSigSuite):
        verify_client_status(GFS_PK, HOME, nonce, Q_SIG, "active", sig, "ed448")


def test_signing_bytes_are_domain_separated_and_suite_bound():
    nonce = new_proof_nonce()
    req = status_request_signing_bytes("AB" * 32, HOME, nonce, "ed25519")
    ans = client_status_signing_bytes(
        "AB" * 32, HOME, nonce, Q_SIG, "active", "ed25519"
    )
    assert req.startswith(CLIENT_STATUS_REQUEST_PREFIX)
    assert ans.startswith(CLIENT_STATUS_SIGNING_PREFIX)
    assert b'"suite":"ed25519"' in req and b'"suite":"ed25519"' in ans
    # The key is lower-cased so a spelling difference can't split it.
    assert req == status_request_signing_bytes("ab" * 32, HOME, nonce, "ed25519")
    assert ans == client_status_signing_bytes(
        "ab" * 32, HOME, nonce, Q_SIG, "active", "ed25519"
    )


def test_sign_client_status_refuses_an_unknown_status():
    with pytest.raises(ValueError):
        sign_client_status(GFS.private_key, HOME, new_proof_nonce(), Q_SIG, "approved")
