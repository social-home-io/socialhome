"""SpaceContentEncryption — per-space epoch-keyed AES-256-GCM (§4.3, §25.8.20–21).

Outbound space content is encrypted with the *current* epoch key; the
epoch number travels in the federation envelope (plaintext) so the
receiver can pick the right decryption key.

Key rotation:

* :meth:`SpaceContentEncryption.rotate_epoch` mints a new 32-byte AES
  key, KEK-encrypts it, persists it, and returns the new epoch
  number.  Triggers: member ban, admin departure, admin promotion,
  scheduled rotation.
* Old epoch keys are kept indefinitely so historical content remains
  decryptable for legitimate readers.

Encryption-first rule (§25.8.21): every space-scoped event MUST be
encrypted unless the federation service needs the field in plaintext
for routing. Routing fields (event_type, from/to, space_id, epoch)
travel in the clear; everything else lands in
``encrypted_payload``.
"""

from __future__ import annotations

import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..federation.sealed_sender import (
    SealedEnvelope,
    UnsealedContent,
    seal_envelope,
    unseal_envelope,
)

from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_space_id,
    generate_space_keypair,
    sign_ed25519,
    verify_ed25519,
)
from ..infrastructure.key_manager import KeyManager
from ..repositories.space_key_repo import (
    AbstractSpaceKeyRepo,
    SpaceKey,
)

log = logging.getLogger(__name__)


_AES_KEY_BYTES = 32
_GCM_NONCE_BYTES = 12


class SpaceContentEncryption:
    """Encrypt/decrypt space content under per-epoch AES-256-GCM keys.

    Parameters
    ----------
    space_key_repo:
        Persistence for epoch keys (KEK-encrypted ciphertext at rest).
    key_manager:
        KEK used to wrap epoch keys before persistence.
    """

    __slots__ = ("_repo", "_kek")

    def __init__(
        self,
        space_key_repo: AbstractSpaceKeyRepo,
        key_manager: KeyManager,
    ) -> None:
        self._repo = space_key_repo
        self._kek = key_manager

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def initialise_for_space(self, space_id: str) -> int:
        """Mint epoch 0 for a brand-new space. Returns the epoch number.

        No-op if the space already has at least one epoch key.
        """
        existing = await self._repo.get_latest(space_id)
        if existing is not None:
            return existing.epoch
        return await self.rotate_epoch(space_id)

    async def rotate_epoch(self, space_id: str) -> int:
        """Generate a fresh AES key, persist it, return the new epoch."""
        epoch = await self._repo.next_epoch(space_id)
        raw = AESGCM.generate_key(bit_length=256)
        wrapped = self._kek.encrypt(raw, associated_data=space_id.encode("utf-8"))
        await self._repo.save(
            SpaceKey(
                space_id=space_id,
                epoch=epoch,
                content_key_hex=wrapped,
            )
        )
        log.info("space_crypto: rotated %s to epoch %d", space_id, epoch)
        return epoch

    async def get_current_epoch(self, space_id: str) -> int | None:
        latest = await self._repo.get_latest(space_id)
        return latest.epoch if latest is not None else None

    # ─── Encrypt / decrypt ────────────────────────────────────────────────

    async def encrypt(self, space_id: str, plaintext: bytes) -> tuple[int, str]:
        """Encrypt under the current epoch. Returns ``(epoch, ciphertext)``.

        Raises :class:`RuntimeError` if no epoch key exists yet — per the
        encryption-first rule in CLAUDE.md, callers should never silently
        fall back to plaintext.
        """
        latest = await self._repo.get_latest(space_id)
        if latest is None:
            raise RuntimeError(
                f"SpaceContentEncryption: no key for space {space_id!r}; "
                "call initialise_for_space() first."
            )
        raw = self._kek.decrypt(
            latest.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        nonce = os.urandom(_GCM_NONCE_BYTES)
        aead = AESGCM(raw)
        ct = aead.encrypt(nonce, plaintext, space_id.encode("utf-8"))
        wire = b64url_encode(nonce) + ":" + b64url_encode(ct)
        return latest.epoch, wire

    async def decrypt(
        self,
        space_id: str,
        epoch: int,
        ciphertext: str,
    ) -> bytes:
        """Decrypt under the specified epoch's key."""
        key = await self._repo.get(space_id, epoch)
        if key is None:
            raise RuntimeError(
                f"SpaceContentEncryption: missing epoch {epoch} for space {space_id!r}"
            )
        raw = self._kek.decrypt(
            key.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        try:
            nonce_b64, ct_b64 = ciphertext.split(":", 1)
        except ValueError as exc:
            raise ValueError("Malformed space ciphertext") from exc
        nonce = b64url_decode(nonce_b64)
        ct = b64url_decode(ct_b64)
        aead = AESGCM(raw)
        return aead.decrypt(nonce, ct, space_id.encode("utf-8"))

    # ─── Sync chunks (§25.6 direct space sync) ──────────────────────────

    async def encrypt_chunk(
        self,
        *,
        space_id: str,
        sync_id: str,
        plaintext: bytes,
    ) -> tuple[int, str]:
        """AES-256-GCM-encrypt a sync chunk under the current epoch key.

        AAD binds to ``space_id:epoch:sync_id`` so a chunk lifted from
        one session can't be replayed into another (§25.8.18). Returns
        ``(epoch, ciphertext)``.
        """
        latest = await self._repo.get_latest(space_id)
        if latest is None:
            raise RuntimeError(
                f"SpaceContentEncryption: no key for space {space_id!r}; "
                "call initialise_for_space() first."
            )
        raw = self._kek.decrypt(
            latest.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        nonce = os.urandom(_GCM_NONCE_BYTES)
        aad = f"{space_id}:{latest.epoch}:{sync_id}".encode("utf-8")
        aead = AESGCM(raw)
        ct = aead.encrypt(nonce, plaintext, aad)
        wire = b64url_encode(nonce) + ":" + b64url_encode(ct)
        return latest.epoch, wire

    async def decrypt_chunk(
        self,
        *,
        space_id: str,
        epoch: int,
        sync_id: str,
        ciphertext: str,
    ) -> bytes:
        """Inverse of :meth:`encrypt_chunk`. Raises :class:`RuntimeError`
        on missing epoch or :class:`cryptography.exceptions.InvalidTag`
        when the AAD doesn't match."""
        key = await self._repo.get(space_id, epoch)
        if key is None:
            raise RuntimeError(
                f"SpaceContentEncryption: missing epoch {epoch} for space {space_id!r}"
            )
        raw = self._kek.decrypt(
            key.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        try:
            nonce_b64, ct_b64 = ciphertext.split(":", 1)
        except ValueError as exc:
            raise ValueError("Malformed space ciphertext") from exc
        nonce = b64url_decode(nonce_b64)
        ct = b64url_decode(ct_b64)
        aad = f"{space_id}:{epoch}:{sync_id}".encode("utf-8")
        aead = AESGCM(raw)
        return aead.decrypt(nonce, ct, aad)

    # ─── Sealed sender (GFS-relayed events §24.10) ──────────────────────

    async def seal_for_gfs(
        self,
        *,
        space_id: str,
        sender_instance_id: str,
        payload_json: str,
    ) -> SealedEnvelope:
        """Wrap an outbound public/global-space event so the GFS relay
        can route it without learning ``from_instance``.

        Returns a :class:`~socialhome.federation.sealed_sender.SealedEnvelope`
        that callers serialise into the ``GFS_POST_RELAY`` payload. The
        space's per-epoch key is unwrapped from the KEK before each call —
        callers never see the raw key material.
        """
        latest = await self._repo.get_latest(space_id)
        if latest is None:
            raise RuntimeError(
                f"seal_for_gfs: no epoch key for space {space_id!r}",
            )
        raw_key = self._kek.decrypt(
            latest.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        sealed = seal_envelope(
            space_id=space_id,
            epoch=latest.epoch,
            sender_instance_id=sender_instance_id,
            payload_json=payload_json,
            space_content_key=raw_key,
        )
        return sealed

    async def unseal_from_gfs(
        self,
        envelope: SealedEnvelope,
    ) -> UnsealedContent:
        """Inverse of :meth:`seal_for_gfs` — fetches the matching epoch
        key and decrypts the sealed envelope.
        """
        key = await self._repo.get(envelope.space_id, envelope.epoch)
        if key is None:
            raise RuntimeError(
                f"unseal_from_gfs: missing epoch {envelope.epoch} for space "
                f"{envelope.space_id!r}",
            )
        raw_key = self._kek.decrypt(
            key.content_key_hex,
            associated_data=envelope.space_id.encode("utf-8"),
        )
        unsealed = unseal_envelope(
            envelope,
            space_content_key=raw_key,
        )
        return unsealed


# ─── Space identity helpers ──────────────────────────────────────────────


def create_space_identity() -> tuple[bytes, bytes, str]:
    """Mint a fresh space identity. Returns ``(seed, public_key, space_id)``.

    Convenience wrapper around :func:`generate_space_keypair` and
    :func:`derive_space_id`.
    """
    kp = generate_space_keypair()
    return kp.private_key, kp.public_key, derive_space_id(kp.public_key)


def sign_space_config(payload: bytes, *, space_seed: bytes) -> str:
    """Ed25519-sign a serialised SpaceConfigEvent (§4.3.4)."""
    sig = sign_ed25519(space_seed, payload)
    return b64url_encode(sig)


def verify_space_config(
    payload: bytes,
    signature_b64: str,
    *,
    space_public_key: bytes,
) -> bool:
    """Verify a config event's Ed25519 signature."""
    try:
        sig = b64url_decode(signature_b64)
    except Exception:
        return False
    return verify_ed25519(space_public_key, payload, sig)
