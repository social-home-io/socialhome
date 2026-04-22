"""Hybrid-signature tests for :class:`FederationEncoder` (§25.8 migration).

Exercises ``sign_envelope_all`` + ``verify_signatures_all`` with a fake
``PqSigner``. Runs without ``liboqs`` installed — the PQ side is
injected via :class:`unittest.mock.MagicMock`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    generate_identity_keypair,
)
from socialhome.federation.encoder import FederationEncoder


def _make_encoder(pq_signer=None, suite="ed25519"):
    kp = generate_identity_keypair()
    encoder = FederationEncoder(
        kp.private_key,
        pq_signer=pq_signer,
        sig_suite=suite,
    )
    return encoder, kp


def _fake_pq_signer(pq_public_key: bytes = b"PQ-PK"):
    """Return a stand-in PqSigner that signs with a reversible marker."""
    signer = MagicMock()
    signer.sign.side_effect = lambda msg: b"PQSIG|" + msg
    return signer, pq_public_key


def test_sign_envelope_all_classical_populates_only_ed25519():
    encoder, _ = _make_encoder(suite="ed25519")
    sigs = encoder.sign_envelope_all(b"some-bytes")
    assert set(sigs.keys()) == {"ed25519"}
    assert len(sigs["ed25519"]) > 0


def test_sign_envelope_all_hybrid_populates_both():
    pq_signer, _ = _fake_pq_signer()
    encoder, _ = _make_encoder(pq_signer=pq_signer, suite="ed25519+mldsa65")
    sigs = encoder.sign_envelope_all(b"some-bytes")
    assert set(sigs.keys()) == {"ed25519", "mldsa65"}


def test_sign_envelope_all_hybrid_without_pq_signer_raises():
    """Configuring the encoder classical but asking for hybrid sign raises."""
    encoder, _ = _make_encoder(suite="ed25519")
    with pytest.raises(RuntimeError, match="no PQ signer"):
        encoder.sign_envelope_all(b"bytes", suite="ed25519+mldsa65")


def test_sign_envelope_all_rejects_unknown_algo_in_suite():
    encoder, _ = _make_encoder(suite="ed25519")
    with pytest.raises(ValueError, match="has no signer wired"):
        encoder.sign_envelope_all(b"bytes", suite="ed25519+banana")


def test_verify_signatures_all_classical_accepts_good_sig():
    encoder, kp = _make_encoder(suite="ed25519")
    msg = b"payload-bytes"
    sigs = encoder.sign_envelope_all(msg)
    ok = encoder.verify_signatures_all(
        msg,
        suite="ed25519",
        signatures=sigs,
        ed_public_key=kp.public_key,
        pq_public_key=None,
    )
    assert ok is True


def test_verify_signatures_all_rejects_tampered_ed_sig():
    encoder, kp = _make_encoder(suite="ed25519")
    msg = b"payload-bytes"
    sigs = encoder.sign_envelope_all(msg)
    bad = bytearray(b64url_decode(sigs["ed25519"]))
    bad[0] ^= 0xFF
    sigs["ed25519"] = b64url_encode(bytes(bad))
    assert (
        encoder.verify_signatures_all(
            msg,
            suite="ed25519",
            signatures=sigs,
            ed_public_key=kp.public_key,
            pq_public_key=None,
        )
        is False
    )


def test_verify_signatures_all_rejects_extra_algo_in_map():
    """signatures map with an algo not in the suite fails the key check."""
    encoder, kp = _make_encoder(suite="ed25519")
    msg = b"payload-bytes"
    sigs = encoder.sign_envelope_all(msg)
    sigs["mldsa65"] = "uninvited"
    assert (
        encoder.verify_signatures_all(
            msg,
            suite="ed25519",
            signatures=sigs,
            ed_public_key=kp.public_key,
            pq_public_key=None,
        )
        is False
    )


def test_verify_signatures_all_rejects_missing_algo_from_map():
    """Hybrid suite requires both signatures — missing one fails."""
    pq_signer, pq_pk = _fake_pq_signer()
    encoder, kp = _make_encoder(pq_signer=pq_signer, suite="ed25519+mldsa65")
    msg = b"payload-bytes"
    sigs = encoder.sign_envelope_all(msg)
    del sigs["mldsa65"]
    assert (
        encoder.verify_signatures_all(
            msg,
            suite="ed25519+mldsa65",
            signatures=sigs,
            ed_public_key=kp.public_key,
            pq_public_key=pq_pk,
        )
        is False
    )


def test_verify_signatures_all_hybrid_requires_pq_pk():
    """suite=ed25519+mldsa65 with pq_public_key=None must fail."""
    pq_signer, _ = _fake_pq_signer()
    encoder, kp = _make_encoder(pq_signer=pq_signer, suite="ed25519+mldsa65")
    msg = b"payload-bytes"
    sigs = encoder.sign_envelope_all(msg)
    assert (
        encoder.verify_signatures_all(
            msg,
            suite="ed25519+mldsa65",
            signatures=sigs,
            ed_public_key=kp.public_key,
            pq_public_key=None,
        )
        is False
    )


def test_verify_signatures_all_rejects_malformed_suite():
    encoder, kp = _make_encoder(suite="ed25519")
    assert (
        encoder.verify_signatures_all(
            b"msg",
            suite="",  # empty suite parses as ValueError
            signatures={"ed25519": "x"},
            ed_public_key=kp.public_key,
            pq_public_key=None,
        )
        is False
    )
