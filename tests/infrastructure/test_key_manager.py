"""Tests for social_home.infrastructure.key_manager."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from social_home.infrastructure.key_manager import KeyManager, KeyManagerError


def test_encrypt_decrypt():
    """Encrypted bytes decrypt back to the original plaintext."""
    km = KeyManager.from_data_dir(Path(tempfile.mkdtemp()))
    wire = km.encrypt(b"secret", associated_data=b"row-1")
    assert km.decrypt(wire, associated_data=b"row-1") == b"secret"


def test_ad_mismatch():
    """Decrypting with mismatched associated_data raises KeyManagerError."""
    km = KeyManager.from_data_dir(Path(tempfile.mkdtemp()))
    wire = km.encrypt(b"secret", associated_data=b"row-1")
    with pytest.raises(KeyManagerError):
        km.decrypt(wire, associated_data=b"row-2")


def test_malformed_wire():
    """Passing a non-valid wire value raises KeyManagerError."""
    km = KeyManager.from_data_dir(Path(tempfile.mkdtemp()))
    with pytest.raises(KeyManagerError):
        km.decrypt("not-valid-format")


def test_stable_across_instances():
    """A key loaded from the same directory decrypts ciphertext from an earlier instance."""
    d = Path(tempfile.mkdtemp())
    km1 = KeyManager.from_data_dir(d)
    wire = km1.encrypt(b"x")
    km2 = KeyManager.from_data_dir(d)
    assert km2.decrypt(wire) == b"x"


def test_bad_kek_length_rejected():
    """KEK with wrong length raises KeyManagerError at construction."""
    with pytest.raises(KeyManagerError):
        KeyManager(b"short")


def test_from_passphrase_produces_working_key():
    """from_passphrase derives a KEK that round-trips encrypt/decrypt."""
    salt = os.urandom(32)
    km = KeyManager.from_passphrase("my-pass", salt)
    wire = km.encrypt(b"secret")
    assert km.decrypt(wire) == b"secret"
