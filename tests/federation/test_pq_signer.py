"""Tests for :class:`PqSigner` — the ML-DSA-65 wrapper.

These tests never require the real ``liboqs`` C library to be
installed — ``oqs`` is stubbed via :data:`sys.modules` injection so the
suite runs in every CI job that does not carry the ``pq`` extra.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from socialhome.federation import pq_signer as pq_signer_module
from socialhome.federation.pq_signer import PqSigner, _OQS_UNAVAILABLE_MSG


class _FakeSignatureCtx:
    """Context-manager stand-in for ``oqs.Signature(...)``.

    Deterministic: signs by returning ``"SIG[" + msg + "]"`` bytes and
    verifies by re-constructing that byte string. Keypair generation
    returns fixed bytes so test assertions stay readable.
    """

    def __init__(self, algorithm: str, secret_key: bytes | None = None) -> None:
        self.algorithm = algorithm
        self.secret_key = secret_key

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def sign(self, message: bytes) -> bytes:
        return b"SIG[" + message + b"]"

    def verify(self, message: bytes, signature: bytes, public_key: bytes) -> bool:
        return signature == b"SIG[" + message + b"]" and public_key == b"PK"

    def generate_keypair(self) -> bytes:
        return b"PK"

    def export_secret_key(self) -> bytes:
        return b"SK"


def _install_fake_oqs(monkeypatch):
    fake = ModuleType("oqs")
    fake.Signature = _FakeSignatureCtx  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "oqs", fake)
    monkeypatch.setattr(pq_signer_module, "_oqs", fake)


def test_sign_and_verify_roundtrip(monkeypatch):
    """PqSigner signs a message and a separate verify accepts it."""
    _install_fake_oqs(monkeypatch)
    signer = PqSigner(secret_key=b"SK")
    msg = b"hello-pq"
    sig = signer.sign(msg)
    assert sig == b"SIG[hello-pq]"
    assert PqSigner.verify(b"PK", msg, sig) is True


def test_verify_rejects_tampered_signature(monkeypatch):
    """A flipped byte in the signature makes verify return False."""
    _install_fake_oqs(monkeypatch)
    signer = PqSigner(secret_key=b"SK")
    sig = bytearray(signer.sign(b"hello-pq"))
    sig[0] ^= 0xFF
    assert PqSigner.verify(b"PK", b"hello-pq", bytes(sig)) is False


def test_verify_rejects_wrong_public_key(monkeypatch):
    """A wrong public key rejects even a valid signature."""
    _install_fake_oqs(monkeypatch)
    signer = PqSigner(secret_key=b"SK")
    sig = signer.sign(b"hello-pq")
    assert PqSigner.verify(b"OTHER-PK", b"hello-pq", sig) is False


def test_generate_keypair_returns_bytes_tuple(monkeypatch):
    """generate_keypair exposes (secret_key, public_key) as bytes."""
    _install_fake_oqs(monkeypatch)
    sk, pk = PqSigner.generate_keypair()
    assert sk == b"SK"
    assert pk == b"PK"


def test_sign_without_liboqs_raises_runtime_error(monkeypatch):
    """When the ``pq`` extra is absent, sign() raises with an install hint."""
    monkeypatch.setattr(pq_signer_module, "_oqs", None)
    signer = PqSigner(secret_key=b"SK")
    with pytest.raises(RuntimeError, match=r"socialhome\[pq\]"):
        signer.sign(b"msg")
    # Message body must be stable for operator-facing scripts.
    assert "liboqs" in _OQS_UNAVAILABLE_MSG


def test_verify_without_liboqs_returns_false(monkeypatch):
    """When the ``pq`` extra is absent, verify() swallows and returns False."""
    monkeypatch.setattr(pq_signer_module, "_oqs", None)
    assert PqSigner.verify(b"PK", b"msg", b"sig") is False


def test_is_available_reflects_oqs_state(monkeypatch):
    """is_available() tracks whether _oqs is loaded."""
    monkeypatch.setattr(pq_signer_module, "_oqs", None)
    assert pq_signer_module.is_available() is False
    monkeypatch.setattr(pq_signer_module, "_oqs", MagicMock())
    assert pq_signer_module.is_available() is True
