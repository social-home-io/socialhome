"""Pure GFS mailbox wire helpers: the id derivation, the domain-separated
signed bytes and the signature suite tag."""

from __future__ import annotations

import base64
import hashlib

import pytest

from socialhome.crypto import INSTANCE_ID_RE, derive_instance_id
from socialhome.domain.gfs_mailbox import (
    MAILBOX_CLAIM_CONTEXT,
    MAILBOX_ID_PREFIX,
    MAILBOX_REGISTER_CONTEXT,
    MAILBOX_SIG_SUITE_ED25519,
    SUPPORTED_MAILBOX_SIG_SUITES,
    InvalidMailboxField,
    UnsupportedMailboxSigSuite,
    claim_signed_bytes,
    derive_mailbox_id,
    is_mailbox_nonce,
    register_signed_bytes,
    validate_mailbox_sig_suite,
)

PK = bytes(range(32))


def test_mailbox_id_is_the_documented_hash_in_instance_id_shape():
    expected = (
        base64.b32encode(hashlib.sha256(b"sh-mbx-v1" + PK).digest())
        .decode()
        .lower()[:32]
    )
    assert derive_mailbox_id(PK) == expected
    assert INSTANCE_ID_RE.fullmatch(derive_mailbox_id(PK))
    assert MAILBOX_ID_PREFIX == b"sh-mbx-v1"


def test_mailbox_id_is_domain_separated_from_the_instance_id():
    """The same key never yields the same id as a household identity."""
    assert derive_mailbox_id(PK) != derive_instance_id(PK)


def test_mailbox_id_refuses_a_key_that_is_not_32_bytes():
    with pytest.raises(ValueError):
        derive_mailbox_id(b"short")


def test_signed_bytes_are_domain_separated_and_bind_every_field():
    mid = derive_mailbox_id(PK)
    reg = register_signed_bytes("gfs-a", mid, 1700000000, "n" * 22)
    claim = claim_signed_bytes("gfs-a", mid, "n" * 22)
    assert reg.startswith(MAILBOX_REGISTER_CONTEXT.encode() + b"\n")
    assert claim.startswith(MAILBOX_CLAIM_CONTEXT.encode() + b"\n")
    assert reg != claim
    assert reg != register_signed_bytes("gfs-b", mid, 1700000000, "n" * 22)
    assert reg != register_signed_bytes("gfs-a", mid, 1700000001, "n" * 22)
    assert reg != register_signed_bytes("gfs-a", mid, 1700000000, "m" * 22)
    assert claim != claim_signed_bytes("gfs-b", mid, "n" * 22)


@pytest.mark.parametrize("bad", ["a\nb", "a b", "", "x" * 300])
def test_signed_bytes_refuse_a_field_that_could_shift_the_framing(bad):
    with pytest.raises(InvalidMailboxField):
        claim_signed_bytes(bad, derive_mailbox_id(PK), "n" * 22)


def test_nonce_shape():
    assert is_mailbox_nonce("A" * 22)
    assert is_mailbox_nonce("a-b_" * 10)
    assert not is_mailbox_nonce("short")
    assert not is_mailbox_nonce("x" * 65)
    assert not is_mailbox_nonce("bad nonce with spaces!!")
    assert not is_mailbox_nonce(12345)


def test_sig_suite_is_tagged_and_unknown_values_are_rejected():
    assert MAILBOX_SIG_SUITE_ED25519 == "ed25519"
    assert MAILBOX_SIG_SUITE_ED25519 in SUPPORTED_MAILBOX_SIG_SUITES
    validate_mailbox_sig_suite("ed25519")
    for bad in ("ed25519+mldsa65", "", None, 1):
        with pytest.raises(UnsupportedMailboxSigSuite):
            validate_mailbox_sig_suite(bad)
    assert issubclass(UnsupportedMailboxSigSuite, ValueError)
