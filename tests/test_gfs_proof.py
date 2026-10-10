"""Tests for :mod:`socialhome.gfs_proof` — GFS proof of possession.

``GET /gfs/info`` is unauthenticated and its capability signature covers only
the instance id + capabilities, so anyone can replay a real GFS's descriptor
from another address. The proof closes that: the household sends a fresh
nonce, and only the holder of the private key can sign it — bound to the base
URL the GFS is configured to be served at, so a relay that forwards the
challenge to the real server still fails at its own address.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from socialhome.crypto import b64url_decode, b64url_encode, generate_identity_keypair
from socialhome.gfs_proof import (
    PROOF_CAPABILITY,
    PROOF_NONCE_BYTES,
    PROOF_ROUTE,
    PROOF_SIG_SUITE_ED25519,
    PROOF_SIGNING_PREFIX,
    SUPPORTED_PROOF_SIG_SUITES,
    UnsupportedProofSigSuite,
    bindable_base_url,
    new_proof_nonce,
    normalize_gfs_base_url,
    parse_proof_nonce,
    proof_signing_bytes,
    sign_possession_proof,
    verify_possession_proof,
)

URL = "https://gfs.example.com"


def _signed(kp, nonce: str, *, gfs_id: str = "gfs-1", url: str = URL):
    return sign_possession_proof(kp.private_key, gfs_id, nonce, url)


def test_constants():
    assert PROOF_SIG_SUITE_ED25519 == "ed25519"
    assert SUPPORTED_PROOF_SIG_SUITES == frozenset({PROOF_SIG_SUITE_ED25519})
    assert PROOF_SIGNING_PREFIX == b"gfs-proof:v1:"
    assert PROOF_NONCE_BYTES == 32
    assert PROOF_CAPABILITY == "proof_of_possession"
    assert PROOF_ROUTE == "/gfs/proof"
    assert issubclass(UnsupportedProofSigSuite, ValueError)


def test_new_nonce_is_32_random_bytes():
    a, b = new_proof_nonce(), new_proof_nonce()
    assert a != b
    assert len(b64url_decode(a)) == PROOF_NONCE_BYTES


def test_sign_then_verify_round_trips():
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    sig, suite = _signed(kp, nonce)
    assert suite == PROOF_SIG_SUITE_ED25519
    assert verify_possession_proof(kp.public_key.hex(), "gfs-1", nonce, URL, sig, suite)


@pytest.mark.parametrize(
    ("gfs_id", "nonce_other", "url"),
    [
        ("gfs-2", False, URL),  # another server's id
        ("gfs-1", True, URL),  # a stale / other nonce (replay)
        ("gfs-1", False, "https://impostor.example.net"),  # another address
        ("gfs-1", False, "http://gfs.example.com"),  # downgraded scheme
    ],
)
def test_every_bound_field_is_checked(gfs_id, nonce_other, url):
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    sig, suite = _signed(kp, nonce)
    check_nonce = new_proof_nonce() if nonce_other else nonce
    assert not verify_possession_proof(
        kp.public_key.hex(), gfs_id, check_nonce, url, sig, suite
    )


def test_wrong_key_fails():
    kp, other = generate_identity_keypair(), generate_identity_keypair()
    nonce = new_proof_nonce()
    sig, suite = _signed(kp, nonce)
    assert not verify_possession_proof(
        other.public_key.hex(), "gfs-1", nonce, URL, sig, suite
    )


def test_unknown_suite_is_rejected_never_defaulted():
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    sig, _ = _signed(kp, nonce)
    with pytest.raises(UnsupportedProofSigSuite):
        verify_possession_proof(kp.public_key.hex(), "gfs-1", nonce, URL, sig, "rsa")


def test_malformed_material_is_false_not_raise():
    nonce = new_proof_nonce()
    assert not verify_possession_proof("zz", "gfs-1", nonce, URL, "sig", "ed25519")
    kp = generate_identity_keypair()
    assert not verify_possession_proof(
        kp.public_key.hex(), "gfs-1", nonce, URL, "!!", "ed25519"
    )


def test_signing_bytes_are_domain_separated_and_bind_the_suite():
    nonce = new_proof_nonce()
    raw = proof_signing_bytes("gfs-1", nonce, URL, PROOF_SIG_SUITE_ED25519)
    assert raw.startswith(PROOF_SIGNING_PREFIX)
    # Not JSON — so it can never collide with a cluster frame (canonical
    # JSON) or the capability block (another prefix) this key also signs.
    assert not raw.startswith(b"{")
    assert raw != proof_signing_bytes("gfs-1", nonce, URL, "ed25519+mldsa65")


def test_url_is_compared_normalized():
    """The GFS's configured URL and the one the household used may differ in
    case, a default port, or a trailing slash — never in substance."""
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    sig, suite = _signed(kp, nonce, url="https://GFS.Example.com:443/")
    assert verify_possession_proof(kp.public_key.hex(), "gfs-1", nonce, URL, sig, suite)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://gfs.example.com", "https://gfs.example.com"),
        ("HTTPS://GFS.example.COM/", "https://gfs.example.com"),
        ("https://gfs.example.com:443", "https://gfs.example.com"),
        ("http://127.0.0.1:80/", "http://127.0.0.1"),
        ("http://127.0.0.1:18765", "http://127.0.0.1:18765"),
        ("https://example.com/gfs/", "https://example.com/gfs"),
        ("http://[::1]:8765", "http://[::1]:8765"),
        ("", ""),
        ("not a url", ""),
        ("ftp://gfs.example.com", ""),
        ("https://gfs.example.com/?q=1", ""),
    ],
)
def test_normalize_gfs_base_url(raw, expected):
    assert normalize_gfs_base_url(raw) == expected


def test_parse_proof_nonce():
    nonce = new_proof_nonce()
    assert parse_proof_nonce(nonce) == nonce
    assert parse_proof_nonce(b64url_encode(b"\x00" * 31)) is None
    assert parse_proof_nonce(b64url_encode(b"\x00" * 33)) is None
    assert parse_proof_nonce("!!!not-base64!!!") is None
    assert parse_proof_nonce(42) is None
    assert parse_proof_nonce("") is None
    assert parse_proof_nonce("A" * 4096) is None


def test_module_stays_dependency_light():
    """Both the content-blind GFS and the household import this module, so it
    may depend only on the stdlib and :mod:`socialhome.crypto`."""
    src = pathlib.Path(__file__).parents[1] / "socialhome" / "gfs_proof.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            assert node.module == "crypto", node.module


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://gfs.example.com/", "https://gfs.example.com"),
        ("http://0.0.0.0:8765", ""),
        ("http://[::]:8765", ""),
        ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("", ""),
    ],
)
def test_bindable_base_url_refuses_wildcard_hosts(raw, expected):
    assert bindable_base_url(raw) == expected
