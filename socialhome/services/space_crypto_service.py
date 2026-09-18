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

# Re-export the pure space-authority signature primitives so every existing
# importer (space_service, federation_inbound_service, private_invite_handler,
# space_config_outbound) keeps importing them from here unchanged. They now
# LIVE in the dependency-light ``socialhome.authority_sig`` module (depends only
# on ``socialhome.crypto``) so the content-blind GFS process can import them
# without dragging in this module's HFS crypto stack (AESGCM, KeyManager).
from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    AUTHORITY_RELAY_EVENT_TYPES,
    AUTHORITY_SIG_SUITE_ED25519,
    SUPPORTED_AUTHORITY_SIG_SUITES,
    UnsupportedAuthoritySuite,
    authority_signing_bytes,
    sign_authority_event,
    strip_authority_sig_fields,
    verify_authority_event,
)
from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_space_id,
    generate_space_keypair,
    sign_ed25519,
    verify_ed25519,
)
from ..domain.events import SpaceContentKeyImported
from ..infrastructure.event_bus import EventBus
from ..infrastructure.key_manager import KeyManager
from ..repositories.space_key_repo import (
    AbstractSpaceKeyRepo,
    SpaceKey,
)
from .bus_publisher import BusPublisherMixin

log = logging.getLogger(__name__)


_AES_KEY_BYTES = 32
_GCM_NONCE_BYTES = 12


#: Symmetric content-key suite identifier shipped alongside the key
#: bytes on the §D1b handoff. Mirrors the ``kem_suite`` convention in
#: :mod:`socialhome.federation.routed_crypto` so the wire shape grows
#: forward-compatibly. When we ever introduce a parallel scheme
#: (e.g. ChaCha20-Poly1305 for low-power receivers, or a PQ-protected
#: variant once Phase-2 of ``docs/crypto.md`` lands), this constant
#: gains a sibling and receivers reject anything they don't know.
#:
#: The AES-256 key itself is already PQ-symmetric-resilient — Grover's
#: algorithm halves effective security to 128 bits, which is still
#: above the 112-bit floor. PQ migration in this codebase is therefore
#: about the *delivery channel* (the §D1b envelope around this
#: payload, see ``docs/crypto.md`` Phase-2), not the AEAD primitive.
KEY_SUITE_AESGCM_256: str = "aesgcm-256"
SUPPORTED_KEY_SUITES: frozenset[str] = frozenset({KEY_SUITE_AESGCM_256})


class UnsupportedKeySuite(ValueError):
    """Raised when an inbound space-content-key payload advertises a
    suite this build doesn't know. Receivers MUST reject rather than
    fall back to a default — otherwise a downgrade attack becomes
    possible once a Phase-2 hybrid scheme lands."""


class SpaceContentEncryption(BusPublisherMixin):
    """Encrypt/decrypt space content under per-epoch AES-256-GCM keys.

    Parameters
    ----------
    space_key_repo:
        Persistence for epoch keys (KEK-encrypted ciphertext at rest).
    key_manager:
        KEK used to wrap epoch keys before persistence.
    own_instance_id:
        This household's instance id. Stamped on every locally-minted epoch
        (:meth:`rotate_epoch`) as ``rotated_by`` so concurrent rotations by
        two delegated admins converge deterministically (Phase 4b). Optional
        only so legacy/test constructions stay valid — a ``None`` minter
        records ``rotated_by = None``, degrading to last-writer-wins.
    """

    __slots__ = (
        "_repo",
        "_kek",
        "_bus",
        "_own_instance_id",
    )

    def __init__(
        self,
        space_key_repo: AbstractSpaceKeyRepo,
        key_manager: KeyManager,
        *,
        bus: EventBus | None = None,
        own_instance_id: str | None = None,
    ) -> None:
        self._repo = space_key_repo
        self._kek = key_manager
        #: Optional — when wired, ``import_key`` publishes
        #: :class:`SpaceContentKeyImported` so :class:`PendingDecryptsCache`
        #: can drain any sync chunks that arrived before this epoch's
        #: key was available (#122, out-of-order key arrival).
        self._bus = bus
        self._own_instance_id = own_instance_id

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
        """Generate a fresh AES key, persist it, return the new epoch.

        Stamps ``rotated_by`` with this household's instance id (Phase 4b) so
        that when two delegated admins rotate to the SAME epoch concurrently,
        every receiver's :meth:`import_key` can converge on the same key by a
        deterministic smallest-``rotated_by`` tiebreak.
        """
        epoch = await self._repo.next_epoch(space_id)
        raw = AESGCM.generate_key(bit_length=256)
        wrapped = self._kek.encrypt(raw, associated_data=space_id.encode("utf-8"))
        await self._repo.save(
            SpaceKey(
                space_id=space_id,
                epoch=epoch,
                content_key_hex=wrapped,
                rotated_by=self._own_instance_id,
            )
        )
        log.info("space_crypto: rotated %s to epoch %d", space_id, epoch)
        return epoch

    async def get_current_epoch(self, space_id: str) -> int | None:
        latest = await self._repo.get_latest(space_id)
        return latest.epoch if latest is not None else None

    # ─── Cross-instance handoff (#117) ────────────────────────────────────

    async def export_current_key(self, space_id: str) -> tuple[int, bytes] | None:
        """Return ``(epoch, raw_aes_key)`` for the §D1b invite envelope.

        Unwraps from KEK so the caller can hand the bytes to the
        federation layer. The envelope is itself encrypted to the
        invitee instance (§D1b zero-leak), so the key never leaves
        this process in plaintext anywhere — but it IS the canonical
        secret that decrypts every event in the space, so callers
        MUST ONLY use this return value inline within an encrypted
        envelope payload. Never persist, never log.

        Returns ``None`` when the space has no keys yet — the
        receiver then falls back to its own ``initialise_for_space``
        on first decrypt attempt (which will fail until the key
        actually federates later, but that's an explicit failure
        rather than a silent decrypt-with-zeros).
        """
        latest = await self._repo.get_latest(space_id)
        if latest is None:
            return None
        raw = self._kek.decrypt(
            latest.content_key_hex,
            associated_data=space_id.encode("utf-8"),
        )
        return latest.epoch, raw

    async def import_key(
        self,
        space_id: str,
        epoch: int,
        raw_key: bytes,
        *,
        rotated_by: str | None = None,
    ) -> None:
        """Persist a key received from a federated peer.

        KEK-wraps with the *receiver's* key manager so the local
        ``space_keys`` row matches the at-rest invariant.

        Collision-safe (Phase 4b): two delegated admins can rotate to the
        SAME epoch concurrently with DIFFERENT keys (each minting epoch N+1
        on a member removal while the owner is offline). A blind upsert would
        let whichever rekey arrived LAST win on each receiver — divergent. So
        when a row already exists at ``(space_id, epoch)`` we keep the
        deterministic winner: the lexicographically **smallest**
        ``rotated_by`` wins, so every receiver converges on the same key
        regardless of arrival order.

        Back-compat (NULL ``rotated_by``): a pre-Phase-4b peer ships no
        ``rotated_by``. An incoming NULL never clobbers an existing row that
        carries one (the stamped winner stands); when BOTH are NULL we
        degrade to today's last-writer-wins. The local stamp on
        :meth:`rotate_epoch` means once everyone ships the field this
        default-on-missing branch becomes the migration tripwire.
        """
        if len(raw_key) != 32:
            raise ValueError("space content key must be 32 bytes")
        existing = await self._repo.get(space_id, epoch)
        if existing is not None and not self._rekey_should_apply(
            existing_rotated_by=existing.rotated_by,
            incoming_rotated_by=rotated_by,
        ):
            log.info(
                "space_crypto: keeping existing epoch %d key for %s "
                "(existing rotated_by=%r beats incoming %r)",
                epoch,
                space_id,
                existing.rotated_by,
                rotated_by,
            )
            return
        wrapped = self._kek.encrypt(
            raw_key,
            associated_data=space_id.encode("utf-8"),
        )
        await self._repo.save(
            SpaceKey(
                space_id=space_id,
                epoch=epoch,
                content_key_hex=wrapped,
                rotated_by=rotated_by,
            )
        )
        log.info(
            "space_crypto: imported epoch %d for %s from peer (rotated_by=%r)",
            epoch,
            space_id,
            rotated_by,
        )
        await self._emit(
            SpaceContentKeyImported(space_id=space_id, epoch=epoch),
        )

    @staticmethod
    def _rekey_should_apply(
        *,
        existing_rotated_by: str | None,
        incoming_rotated_by: str | None,
    ) -> bool:
        """Decide whether an incoming rekey replaces an existing same-epoch row.

        Deterministic convergence rule (Phase 4b):

        * existing has no minter (NULL):
            - incoming has one → APPLY (the stamped winner supersedes the
              legacy/unknown row).
            - incoming also NULL → APPLY (last-writer-wins, legacy contract).
        * existing has a minter:
            - incoming NULL → KEEP existing (an older peer must not clobber a
              deterministic winner).
            - incoming has one → APPLY only if it sorts STRICTLY SMALLER
              (smallest ``rotated_by`` wins; equal/own row → no-op rewrite is
              harmless but we skip it to avoid a redundant write + event).
        """
        if existing_rotated_by is None:
            return True
        if incoming_rotated_by is None:
            return False
        return incoming_rotated_by < existing_rotated_by

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


# ─── Space-authority signature primitives ─────────────────────────────────
#
# Moved to the dependency-light ``socialhome.authority_sig`` module and
# re-exported from the import block at the top of this file (see there). The
# names below stay importable from ``space_crypto_service`` for back-compat.

__all__ = [
    "AUTHORITY_EVENT_SPACE_POST_PUBLIC",
    "AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF",
    "AUTHORITY_RELAY_EVENT_TYPES",
    "AUTHORITY_SIG_SUITE_ED25519",
    "SUPPORTED_AUTHORITY_SIG_SUITES",
    "KEY_SUITE_AESGCM_256",
    "SUPPORTED_KEY_SUITES",
    "SpaceContentEncryption",
    "UnsupportedAuthoritySuite",
    "UnsupportedKeySuite",
    "authority_signing_bytes",
    "create_space_identity",
    "sign_authority_event",
    "sign_space_config",
    "strip_authority_sig_fields",
    "verify_authority_event",
    "verify_space_config",
]
