"""Tests for writer group key derivation, certs and signatures (v_50)."""

from __future__ import annotations

import dataclasses
import os

import pytest

from socialhome.crypto import b64url_encode, ed25519_public_key, sign_ed25519
from socialhome.domain.writer_key import UnsupportedWriterKeySuite
from socialhome.writer_key import (
    InvalidWriterKey,
    derive_writer_seed,
    issue_writer_key_grant,
    sign_with_writer_key,
    sign_writer_key_cert,
    verify_writer_key_cert,
    verify_writer_key_grant,
    verify_writer_sig,
    writer_key_cert_signing_bytes,
)

SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)


def test_derivation_is_deterministic_per_space_and_epoch() -> None:
    a = derive_writer_seed(SPACE_SEED, "sp", 4)
    assert a == derive_writer_seed(SPACE_SEED, "sp", 4)
    assert len(a) == 32
    assert a != derive_writer_seed(SPACE_SEED, "sp", 5)
    assert a != derive_writer_seed(SPACE_SEED, "other", 4)
    assert a != derive_writer_seed(os.urandom(32), "sp", 4)
    # Never the authority seed itself.
    assert a != SPACE_SEED


def test_derivation_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        derive_writer_seed(b"short", "sp", 1)
    with pytest.raises(ValueError):
        derive_writer_seed(SPACE_SEED, "sp", -1)
    with pytest.raises(ValueError):
        derive_writer_seed(SPACE_SEED, "sp", True)


def test_cert_verifies_and_returns_the_writer_pk() -> None:
    pk = ed25519_public_key(os.urandom(32))
    cert = sign_writer_key_cert(
        space_seed=SPACE_SEED, space_id="sp", epoch=2, writer_pk=pk
    )
    assert (
        verify_writer_key_cert(cert, space_pubkey=SPACE_PK, space_id="sp", epoch=2)
        == pk
    )


def test_cert_rejects_wrong_key_space_epoch_and_tamper() -> None:
    pk = ed25519_public_key(os.urandom(32))
    cert = sign_writer_key_cert(
        space_seed=SPACE_SEED, space_id="sp", epoch=2, writer_pk=pk
    )
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_cert(
            cert,
            space_pubkey=ed25519_public_key(os.urandom(32)),
            space_id="sp",
            epoch=2,
        )
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_cert(cert, space_pubkey=SPACE_PK, space_id="other", epoch=2)
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_cert(cert, space_pubkey=SPACE_PK, space_id="sp", epoch=3)
    forged = dataclasses.replace(cert, epoch=9)
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_cert(forged, space_pubkey=SPACE_PK, space_id="sp", epoch=9)
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_cert(
            dataclasses.replace(cert, cert_sig="!!"),
            space_pubkey=SPACE_PK,
            space_id="sp",
            epoch=2,
        )
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_cert(cert, space_pubkey=b"x", space_id="sp", epoch=2)


def test_cert_rejects_unknown_suite() -> None:
    pk = ed25519_public_key(os.urandom(32))
    cert = sign_writer_key_cert(
        space_seed=SPACE_SEED, space_id="sp", epoch=2, writer_pk=pk
    )
    with pytest.raises(UnsupportedWriterKeySuite):
        verify_writer_key_cert(
            dataclasses.replace(cert, writer_key_suite="ed25519+mldsa65"),
            space_pubkey=SPACE_PK,
            space_id="sp",
            epoch=2,
        )


def test_sign_cert_rejects_bad_pk() -> None:
    with pytest.raises(ValueError):
        sign_writer_key_cert(
            space_seed=SPACE_SEED, space_id="sp", epoch=1, writer_pk=b"x"
        )


def test_cert_with_short_writer_pk_is_refused() -> None:
    cert = sign_writer_key_cert(
        space_seed=SPACE_SEED, space_id="sp", epoch=1, writer_pk=os.urandom(32)
    )
    short = dataclasses.replace(cert, writer_pk=b64url_encode(b"abc"))
    resigned = dataclasses.replace(
        short,
        cert_sig=b64url_encode(
            sign_ed25519(SPACE_SEED, writer_key_cert_signing_bytes(short))
        ),
    )
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_cert(resigned, space_pubkey=SPACE_PK, space_id="sp", epoch=1)


def test_grant_round_trip_verifies() -> None:
    grant = issue_writer_key_grant(space_seed=SPACE_SEED, space_id="sp", epoch=7)
    seed = verify_writer_key_grant(grant, space_pubkey=SPACE_PK, space_id="sp")
    assert seed == derive_writer_seed(SPACE_SEED, "sp", 7)


def test_grant_rejects_mismatch() -> None:
    grant = issue_writer_key_grant(space_seed=SPACE_SEED, space_id="sp", epoch=7)
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_grant(grant, space_pubkey=SPACE_PK, space_id="other")
    swapped = dataclasses.replace(grant, writer_seed=b64url_encode(os.urandom(32)))
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_grant(swapped, space_pubkey=SPACE_PK, space_id="sp")
    bad = dataclasses.replace(grant, writer_seed="!!")
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_grant(bad, space_pubkey=SPACE_PK, space_id="sp")
    # Epoch of the grant must be the cert's.
    shifted = dataclasses.replace(grant, epoch=8)
    with pytest.raises(InvalidWriterKey):
        verify_writer_key_grant(shifted, space_pubkey=SPACE_PK, space_id="sp")
    with pytest.raises(UnsupportedWriterKeySuite):
        verify_writer_key_grant(
            dataclasses.replace(grant, writer_key_suite="nope"),
            space_pubkey=SPACE_PK,
            space_id="sp",
        )


def test_writer_sig_round_trip() -> None:
    seed = derive_writer_seed(SPACE_SEED, "sp", 1)
    pk = ed25519_public_key(seed)
    sig = sign_with_writer_key(seed, b"hello")
    assert verify_writer_sig(
        writer_pk=pk, message=b"hello", writer_sig=sig, suite="ed25519"
    )
    assert not verify_writer_sig(
        writer_pk=pk, message=b"other", writer_sig=sig, suite="ed25519"
    )
    assert not verify_writer_sig(
        writer_pk=pk, message=b"hello", writer_sig="!!", suite="ed25519"
    )
    with pytest.raises(UnsupportedWriterKeySuite):
        verify_writer_sig(writer_pk=pk, message=b"hello", writer_sig=sig, suite="rsa")
