"""Local-at-rest key encryption (§25).

The ``KeyManager`` owns the Key Encryption Key (KEK) used to encrypt
long-lived secrets that the instance stores on disk:

* The Ed25519 identity private key seed (``instance_identity``).
* Per-peer AES-GCM session keys (``remote_instances``).
* Per-space content keys (``space_keys``).
* Ephemeral DH private keys held during pairing (``pending_pairings``).

**Wire format.** Each encrypted value is a single string:
``b64url(nonce) + ":" + b64url(ciphertext_with_tag)``.

**Not part of a federation protocol.** These blobs never leave the host —
the KEK exists only to raise the bar against an attacker who reads the
SQLite file off a backup disk. A determined attacker with root on the live
machine can still extract both the KEK and the plaintext.

**KEK source.** In HA App mode the KEK is derived from a host-unique salt
written to ``/data/.kek_salt`` on first start (Supervisor keeps ``/data``
intact across updates). In standalone mode the operator may pass a
passphrase via ``SOCIAL_HOME_KEK_PASSPHRASE``; if absent we fall back to the
on-disk salt the same way.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from ..crypto import b64url_decode, b64url_encode


class KeyManagerError(Exception):
    """Raised when a KEK operation fails (decrypt mismatch, bad format, …)."""


class KeyManager:
    """AES-256-GCM envelope encryption using a long-lived KEK.

    ``kek`` is 32 raw bytes. Prefer building instances via
    :meth:`from_data_dir` or :meth:`from_passphrase` rather than passing the
    raw bytes directly — those factory methods handle salt persistence and
    key derivation consistently.
    """

    __slots__ = ("_aead",)

    NONCE_BYTES = 12  # AES-GCM standard
    KEK_BYTES = 32

    def __init__(self, kek: bytes) -> None:
        if len(kek) != self.KEK_BYTES:
            raise KeyManagerError(f"KEK must be {self.KEK_BYTES} bytes, got {len(kek)}")
        self._aead = AESGCM(kek)

    # ── Factories ────────────────────────────────────────────────────────

    @classmethod
    def from_data_dir(cls, data_dir: str | Path) -> "KeyManager":
        """Build a KEK from a 32-byte salt stored under ``data_dir``.

        The salt file is created with mode ``0o600`` on first call. Later
        calls read the existing salt so the KEK is stable across restarts
        on the same host.
        """
        path = Path(data_dir) / ".kek_salt"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(os.urandom(cls.KEK_BYTES))
            os.chmod(path, 0o600)
        salt = path.read_bytes()
        if len(salt) != cls.KEK_BYTES:
            raise KeyManagerError(
                f"KEK salt at {path} has unexpected length {len(salt)}",
            )
        return cls(_derive(salt, b"socialhome/kek/from-data-dir"))

    @classmethod
    def from_passphrase(
        cls,
        passphrase: str,
        salt: bytes,
    ) -> "KeyManager":
        """Derive a KEK from a passphrase and a stable 32-byte salt."""
        if len(salt) != cls.KEK_BYTES:
            raise KeyManagerError("Passphrase salt must be 32 bytes")
        return cls(
            _derive(
                passphrase.encode("utf-8") + b":" + salt, b"socialhome/kek/passphrase"
            )
        )

    # ── Encrypt / decrypt ────────────────────────────────────────────────

    def encrypt(self, plaintext: bytes, *, associated_data: bytes | None = None) -> str:
        """AES-GCM-encrypt ``plaintext``; return ``b64url(nonce):b64url(ct)``.

        ``associated_data`` is authenticated but not encrypted. Use it to
        bind a ciphertext to a stable context (e.g. the row's primary key)
        so a stolen ciphertext cannot be swapped into a different row.
        """
        nonce = os.urandom(self.NONCE_BYTES)
        ct = self._aead.encrypt(nonce, plaintext, associated_data)
        return f"{b64url_encode(nonce)}:{b64url_encode(ct)}"

    def decrypt(self, wire: str, *, associated_data: bytes | None = None) -> bytes:
        """Inverse of :meth:`encrypt`.

        Raises :class:`KeyManagerError` on format or authentication failure.
        Nothing exception-chained is logged — the caller decides how to
        surface a decrypt failure.
        """
        try:
            nonce_b, ct_b = wire.split(":", 1)
            nonce = b64url_decode(nonce_b)
            ct = b64url_decode(ct_b)
        except ValueError as exc:
            raise KeyManagerError("Malformed KEK wire format") from exc
        if len(nonce) != self.NONCE_BYTES:
            raise KeyManagerError(f"Unexpected nonce length {len(nonce)}")
        try:
            return self._aead.decrypt(nonce, ct, associated_data)
        except Exception as exc:
            raise KeyManagerError("KEK decrypt failed") from exc


# ─── Internal helpers ─────────────────────────────────────────────────────


def _derive(material: bytes, info: bytes) -> bytes:
    """HKDF-SHA256 → 32 raw bytes.

    Not exported. The KEK is not a user-visible type.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    )
    return hkdf.derive(material)
